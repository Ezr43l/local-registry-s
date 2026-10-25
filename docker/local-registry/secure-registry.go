// Copyright 2026 Ezrael. SPDX-License-Identifier: Apache-2.0
// The public OCI listener authenticates machine credentials independently of the panel.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

type credential struct {
	ID string `json:"id"`
	Name string `json:"name"`
	Username string `json:"username"`
	Hash string `json:"token_hash"`
	Permissions []string `json:"permissions"`
	Repositories []string `json:"repositories"`
}
type policy struct {
	Schema int `json:"schema"`
	Networks []string `json:"allowed_networks"`
	Names []string `json:"tls_names"`
	Clients []credential `json:"clients"`
	Generation int64 `json:"generation"`
}

func readPrivate(path string) ([]byte,error) {
	f,err:=os.Open(path); if err!=nil{return nil,err}; defer f.Close()
	st,err:=f.Stat(); if err!=nil{return nil,err}
	lst,err:=os.Lstat(path); if err!=nil{return nil,err}
	if !st.Mode().IsRegular() || lst.Mode()&os.ModeSymlink!=0 || st.Mode().Perm()&0077!=0 || st.Size()>1024*1024 {return nil,errors.New("unsafe private file")}
	return os.ReadFile(path)
}
func writePrivate(path string,data []byte) error {
	f,err:=os.CreateTemp(filepath.Dir(path),".secure-");if err!=nil{return err}
	tmp:=f.Name();defer os.Remove(tmp)
	if err=f.Chmod(0600);err==nil{_,err=f.Write(data)}
	if err==nil{err=f.Sync()};closeErr:=f.Close();if err==nil{err=closeErr}
	if err!=nil{return err};return os.Rename(tmp,path)
}
func serial() *big.Int { n,err:=rand.Int(rand.Reader,new(big.Int).Lsh(big.NewInt(1),128));if err!=nil{panic(err)};return n }
func initCertificates(dir,names string) error {
	if err:=os.MkdirAll(dir,0700);err!=nil{return err}
	st,err:=os.Lstat(dir);if err!=nil{return err};if !st.IsDir()||st.Mode()&os.ModeSymlink!=0{return errors.New("unsafe security directory")}
	caPath,keyPath:=filepath.Join(dir,"ca.pem"),filepath.Join(dir,"ca-key.pem")
	caPEM,e1:=readPrivate(caPath);keyPEM,e2:=readPrivate(keyPath)
	var ca *x509.Certificate;var caKey *ecdsa.PrivateKey
	if os.IsNotExist(e1)&&os.IsNotExist(e2) {
		caKey,err=ecdsa.GenerateKey(elliptic.P256(),rand.Reader);if err!=nil{return err}
		t:=&x509.Certificate{SerialNumber:serial(),Subject:pkix.Name{CommonName:"Local Registry private CA"},NotBefore:time.Now().Add(-5*time.Minute),NotAfter:time.Now().AddDate(10,0,0),IsCA:true,BasicConstraintsValid:true,KeyUsage:x509.KeyUsageCertSign|x509.KeyUsageCRLSign}
		der,err:=x509.CreateCertificate(rand.Reader,t,t,&caKey.PublicKey,caKey);if err!=nil{return err}
		ca,err=x509.ParseCertificate(der);if err!=nil{return err}
		keyDER,err:=x509.MarshalECPrivateKey(caKey);if err!=nil{return err}
		if err=writePrivate(keyPath,pem.EncodeToMemory(&pem.Block{Type:"EC PRIVATE KEY",Bytes:keyDER}));err!=nil{return err}
		if err=writePrivate(caPath,pem.EncodeToMemory(&pem.Block{Type:"CERTIFICATE",Bytes:der}));err!=nil{return err}
	} else {
		if e1!=nil{return e1};if e2!=nil{return e2}
		b,_:=pem.Decode(caPEM);if b==nil{return errors.New("invalid CA certificate")};ca,err=x509.ParseCertificate(b.Bytes);if err!=nil{return err}
		b,_=pem.Decode(keyPEM);if b==nil{return errors.New("invalid CA key")};caKey,err=x509.ParseECPrivateKey(b.Bytes);if err!=nil{return err}
		if !ca.IsCA||!caKey.PublicKey.Equal(ca.PublicKey){return errors.New("CA key mismatch")}
	}
	leafKey,err:=ecdsa.GenerateKey(elliptic.P256(),rand.Reader);if err!=nil{return err}
	leaf:=&x509.Certificate{SerialNumber:serial(),Subject:pkix.Name{CommonName:"Local Registry"},NotBefore:time.Now().Add(-5*time.Minute),NotAfter:time.Now().AddDate(1,0,0),KeyUsage:x509.KeyUsageDigitalSignature,ExtKeyUsage:[]x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	seen:=map[string]bool{};dns:=regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$`)
	for _,n:=range strings.Split("localhost,127.0.0.1,::1,"+names,",") {
		n=strings.TrimSpace(n);if n==""||seen[n]{continue};seen[n]=true
		if ip:=net.ParseIP(n);ip!=nil{leaf.IPAddresses=append(leaf.IPAddresses,ip)}else if dns.MatchString(n){leaf.DNSNames=append(leaf.DNSNames,n)}else{return errors.New("invalid certificate name")}
	}
	der,err:=x509.CreateCertificate(rand.Reader,leaf,ca,&leafKey.PublicKey,caKey);if err!=nil{return err}
	keyDER,err:=x509.MarshalECPrivateKey(leafKey);if err!=nil{return err}
	// Publish one bundle atomically; the listener cannot observe a mismatched pair.
	bundle:=append(pem.EncodeToMemory(&pem.Block{Type:"CERTIFICATE",Bytes:der}),pem.EncodeToMemory(&pem.Block{Type:"EC PRIVATE KEY",Bytes:keyDER})...)
	return writePrivate(filepath.Join(dir,"server.pem"),bundle)
}
func loadPolicy(dir string) (*policy,error) {
	raw,err:=readPrivate(filepath.Join(dir,"policy.json"));if err!=nil{return nil,err}
	var p policy;if err=json.Unmarshal(raw,&p);err!=nil{return nil,err}
	if p.Schema!=1||len(p.Networks)==0||len(p.Networks)>64||len(p.Clients)>128{return nil,errors.New("invalid security policy")}
	for _,n:=range p.Networks{if _,_,err:=net.ParseCIDR(n);err!=nil{return nil,err}}
	for _,c:=range p.Clients{if len(c.Hash)!=64||len(c.Username)>64||c.Username==""{return nil,errors.New("invalid credential")};if _,err:=hex.DecodeString(c.Hash);err!=nil{return nil,err}}
	return &p,nil
}
func allowedIP(p *policy,remote string) bool {
	host,_,err:=net.SplitHostPort(remote);if err!=nil{return false};ip:=net.ParseIP(host);if ip==nil{return false}
	for _,s:=range p.Networks{_,network,err:=net.ParseCIDR(s);if err==nil&&network.Contains(ip){return true}};return false
}
func hasPermission(c credential,action,repo string) bool {
	permission:=false;for _,a:=range c.Permissions{if a==action||a=="admin"{permission=true}}
	if !permission{return false}
	for _,r:=range c.Repositories{if r=="*"||r==repo||(strings.HasSuffix(r,"/*")&&strings.HasPrefix(repo,strings.TrimSuffix(r,"*") )){return true}};return false
}
var repoPattern=regexp.MustCompile(`^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$`)
func requestScope(r *http.Request) (string,string,error) {
	if strings.Contains(strings.ToLower(r.URL.EscapedPath()),"%2f")||strings.Contains(r.URL.Path,"\\")||strings.Contains(r.URL.Path,"..") {return "","",errors.New("ambiguous path")}
	if r.URL.Path=="/v2/"||r.URL.Path=="/v2"{return "ping","*",nil}
	if r.URL.Path=="/v2/_catalog"{return "pull","*",nil}
	if !strings.HasPrefix(r.URL.Path,"/v2/"){return "","",errors.New("unknown endpoint")}
	rest:=strings.TrimPrefix(r.URL.Path,"/v2/");repo:=""
	for _,part:=range []string{"/manifests/","/blobs/","/tags/list","/referrers/"}{if i:=strings.LastIndex(rest,part);i>0{repo=rest[:i];break}}
	if !repoPattern.MatchString(repo){return "","",errors.New("invalid repository")}
	switch r.Method{case "GET","HEAD":return "pull",repo,nil;case "POST","PUT","PATCH":return "push",repo,nil;case "DELETE":return "delete",repo,nil};return "","",errors.New("unsupported method")
}
type limiter struct {mu sync.Mutex; entries map[string]struct{at time.Time;n int}}
func (l *limiter) allow(remote string) bool {
	host,_,_:=net.SplitHostPort(remote);now:=time.Now();l.mu.Lock();defer l.mu.Unlock()
	for k,v:=range l.entries{if now.Sub(v.at)>time.Minute{delete(l.entries,k)}}
	x:=l.entries[host];if x.at.IsZero(){if len(l.entries)>=4096{return false};x.at=now};x.n++;l.entries[host]=x;return x.n<=60
}
type boundedListener struct {net.Listener; slots chan struct{}}
type boundedConnection struct {net.Conn; once sync.Once; release func()}
func (c *boundedConnection) Close() error {err:=c.Conn.Close();c.once.Do(c.release);return err}
func (l *boundedListener) Accept()(net.Conn,error){
	l.slots<-struct{}{}
	c,err:=l.Listener.Accept();if err!=nil{<-l.slots;return nil,err}
	return &boundedConnection{Conn:c,release:func(){<-l.slots}},nil
}
func serve(dir,addr,backend string) error {
	target,err:=url.Parse(backend);if err!=nil||target.Scheme!="http"||target.Host!="127.0.0.1:5050"{return errors.New("backend must be the private loopback registry")}
	proxy:=httputil.NewSingleHostReverseProxy(target)
	proxy.Transport=&http.Transport{Proxy:nil,MaxIdleConns:32,MaxIdleConnsPerHost:16,IdleConnTimeout:30*time.Second,ResponseHeaderTimeout:30*time.Second}
	proxy.ErrorHandler=func(w http.ResponseWriter,r *http.Request,e error){http.Error(w,"Registry unavailable",http.StatusBadGateway)}
	capacity:=make(chan struct{},32);failures:=&limiter{entries:map[string]struct{at time.Time;n int}{}}
	handler:=http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){
		w.Header().Set("Docker-Distribution-Api-Version","registry/2.0");w.Header().Set("X-Content-Type-Options","nosniff")
		p,err:=loadPolicy(dir);if err!=nil{http.Error(w,"Security configuration unavailable",503);return}
		if !allowedIP(p,r.RemoteAddr){log.Printf(`{"event":"registry_network_denied","source":%q}`,r.RemoteAddr);http.Error(w,"Forbidden",403);return}
		if r.URL.Path=="/healthz"{host,_,_:=net.SplitHostPort(r.RemoteAddr);if ip:=net.ParseIP(host);ip!=nil&&ip.IsLoopback(){w.WriteHeader(200);return}}
		select{case capacity<-struct{}{}:defer func(){<-capacity}();default:http.Error(w,"Too many connections",503);return}
		username,password,ok:=r.BasicAuth();var client *credential
		digest:=sha256.Sum256([]byte(password));want:=hex.EncodeToString(digest[:])
		if ok&&len(password)>=32&&len(password)<=256 {for _,c:=range p.Clients{if subtle.ConstantTimeCompare([]byte(c.Username),[]byte(username))==1&&subtle.ConstantTimeCompare([]byte(c.Hash),[]byte(want))==1{x:=c;client=&x}}}
		if client==nil{
			if !failures.allow(r.RemoteAddr){w.Header().Set("Retry-After","60");http.Error(w,"Too many authentication attempts",429);return}
			log.Printf(`{"event":"registry_auth_denied","source":%q}`,r.RemoteAddr);w.Header().Set("WWW-Authenticate",`Basic realm="Local Registry", charset="UTF-8"`);http.Error(w,"Authentication required",401);return
		}
		action,repo,err:=requestScope(r);if err!=nil{http.Error(w,"Invalid registry request",400);return}
		if action!="ping"&&!hasPermission(*client,action,repo){log.Printf(`{"event":"registry_permission_denied","client":%q,"action":%q}`,client.ID,action);http.Error(w,"Insufficient permissions",403);return}
		if from:=r.URL.Query().Get("from");from!=""&&!hasPermission(*client,"pull",from){http.Error(w,"Source repository not permitted",403);return}
		r.Body=http.MaxBytesReader(w,r.Body,20<<30)
		r.Header.Del("Authorization");r.Header.Del("Proxy-Authorization");r.Header.Del("Forwarded");r.Header.Del("X-Forwarded-For");r.Header.Del("X-Forwarded-Host");r.Header.Del("X-Forwarded-Proto")
		r.Header.Set("X-Forwarded-Proto","https")
		if action!="pull"&&action!="ping"{log.Printf(`{"event":"registry_write","client":%q,"action":%q,"repository":%q,"source":%q}`,client.ID,action,repo,r.RemoteAddr)}
		proxy.ServeHTTP(w,r)
	})
	var certMu sync.Mutex
	getCert:=func(*tls.ClientHelloInfo)(*tls.Certificate,error){certMu.Lock();defer certMu.Unlock();b,err:=readPrivate(filepath.Join(dir,"server.pem"));if err!=nil{return nil,err};c,err:=tls.X509KeyPair(b,b);return &c,err}
	srv:=&http.Server{Addr:addr,Handler:handler,ReadHeaderTimeout:10*time.Second,ReadTimeout:30*time.Minute,WriteTimeout:30*time.Minute,IdleTimeout:30*time.Second,MaxHeaderBytes:16384,TLSConfig:&tls.Config{MinVersion:tls.VersionTLS12,GetCertificate:getCert}}
	listener,err:=net.Listen("tcp",addr);if err!=nil{return err}
	limited:=&boundedListener{Listener:listener,slots:make(chan struct{},64)}
	return srv.Serve(tls.NewListener(limited,srv.TLSConfig))
}
func main(){
	dir:=flag.String("dir","/var/lib/registry/.local-registry/security","private configuration directory")
	mode:=flag.String("mode","serve","init or serve")
	names:=flag.String("names","","certificate names, comma separated")
	addr:=flag.String("addr",":5000","public TLS listener")
	backend:=flag.String("backend","http://127.0.0.1:5050","private registry")
	flag.Parse();var err error
	if *mode=="init"{err=initCertificates(*dir,*names)}else if *mode=="serve"{err=serve(*dir,*addr,*backend)}else{err=fmt.Errorf("unknown mode")}
	if err!=nil{log.Fatal(err)}
}
