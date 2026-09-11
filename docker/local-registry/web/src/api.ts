// El contrato con el backend, en un solo sitio.
//
// Todo lo que la interfaz sabe del registro entra por aquí. Si algún día se
// cambia React por otra cosa, este fichero y la API siguen siendo válidos: la
// página es un consumidor, no parte del servicio.

export const APP_VERSION = import.meta.env.VITE_APP_VERSION || 'desarrollo'

export interface AuthSession {
  username: string
  display_name: string
  csrf_token: string
  expires_at: number
}

export interface AuthStatus {
  setup_required: boolean
  authenticated: boolean
  session: AuthSession | null
}

async function authRequest(path: string, payload: Record<string, string>): Promise<AuthStatus> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok || !body.authenticated) {
    throw new Error(body.error || `El servicio respondió ${response.status}`)
  }
  return { setup_required: false, authenticated: true, session: body.session }
}

export async function fetchAuth(): Promise<AuthStatus> {
  const response = await fetch('api/auth', { cache: 'no-store', credentials: 'same-origin' })
  if (!response.ok) throw new Error(`El servicio respondió ${response.status}`)
  return response.json()
}

export async function createOwner(payload: {
  username: string
  display_name: string
  password: string
  password_confirmation: string
}): Promise<AuthStatus> {
  return authRequest('api/auth/setup', payload)
}

export async function login(username: string, password: string): Promise<AuthStatus> {
  return authRequest('api/auth/session', { username, password })
}

export async function logout(): Promise<void> {
  const response = await fetch('api/auth/session', {
    method: 'DELETE', credentials: 'same-origin',
  })
  if (!response.ok) throw new Error(`El servicio respondió ${response.status}`)
}

export interface SetupStatus { required: boolean; version: string }
export interface SetupMember { name: string; registry_url: string; panel_url: string }
export interface SetupPayload {
  node: string
  members: SetupMember[]
  timezone: string
  enrollment_code: string
  cache_ttl: number
  cors_allowed_origins: string
  maintenance: {
    enabled: boolean
    keep_last: number
    hour: string
    gc: boolean
    coordinator: string
    protected_tags: string
    sync_source: string
    lease_ttl: number
    require_all_nodes: boolean
    branch_pattern: string
  }
}

export async function fetchSetup(): Promise<SetupStatus> {
  const response = await fetch('api/setup', { cache: 'no-store' })
  if (!response.ok) throw new Error(`El servicio respondió ${response.status}`)
  return response.json()
}

export async function saveSetup(payload: SetupPayload, csrfToken: string): Promise<{
  ok: boolean; restarting: boolean; enrollment_code: string
}> {
  const response = await fetch('api/setup', {
    method: 'POST', credentials: 'same-origin', headers: {
      'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify(payload),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok || !body.ok) throw new Error(body.error || `El servicio respondió ${response.status}`)
  return body
}

export async function fetchSettings(): Promise<SetupPayload> {
  const response = await fetch('api/settings', { cache: 'no-store', credentials: 'same-origin' })
  if (!response.ok) throw new Error(`El servicio respondió ${response.status}`)
  return response.json()
}

export async function saveSettings(payload: SetupPayload, csrfToken: string): Promise<{
  ok: boolean; restarting: boolean
}> {
  const response = await fetch('api/settings', {
    method: 'POST', credentials: 'same-origin', headers: {
      'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify(payload),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok || !body.ok) throw new Error(body.error || `El servicio respondió ${response.status}`)
  return body
}

export interface Project {
  name: string
  tags: number
  blobs: number
  /** Blobs que SOLO usa este proyecto: lo que se recupera si se borra entero. */
  exclusive_bytes: number
  /** Blobs compartidos con otros proyectos: borrarlo no los libera. */
  shared_bytes: number
  total_bytes: number
  tag_names: string[]
}

export interface Summary {
  projects: number
  tags: number
  blobs_on_disk: number
  blobs_referenced: number
  total_bytes: number
  /** Blobs en disco que ninguna etiqueta referencia: candidatos a recolectar. */
  orphan_blobs: number
  orphan_bytes: number
  /** Referenciados pero ausentes del disco. Distinto de cero = algo va mal. */
  missing_blobs: number
}

export interface DriftIssue {
  project: string
  tag: string
  /** digest abreviado por nodo; null = ese nodo no tiene la etiqueta */
  nodes: Record<string, string | null>
}

export interface Drift {
  nodes: string[]
  /** Ninguna etiqueta apunta a imágenes distintas según el nodo. La señal real. */
  consistent: boolean
  /** Misma etiqueta, imagen distinta. Siempre es un problema. */
  mismatch_count: number
  mismatches: DriftIssue[]
  /** Etiqueta presente solo en algunos nodos. Normalmente esperado. */
  missing_count: number
  missing: DriftIssue[]
}

export interface GcResult {
  action: 'gc'
  dry_run: boolean
  ok: boolean
  bytes_before: number
  bytes_after: number
  freed_bytes: number
  registry_back: boolean
  output: string
  at: number
  cluster?: boolean
  nodes?: string[]
  node_results?: Record<string, GcNodeResult>
  error?: string
}

export interface GcNodeResult {
  action: 'gc'
  dry_run: boolean
  ok: boolean
  bytes_before?: number
  bytes_after?: number
  freed_bytes: number
  registry_back: boolean
  output?: string
  error?: string
  skipped?: boolean
  reason?: string
}

/** Una rama dentro de un proyecto: 'stable', 'dev', 'rc'… La retención cuenta
 *  las N últimas DE CADA UNA, no las N últimas en total. */
export interface RetentionBranch {
  branch: string
  keeping: string[]
  removing: string[]
  /** etiqueta → fecha de construcción (epoch), sacada de la propia imagen */
  dates: Record<string, number>
}

export interface RetentionCandidate {
  project: string
  tags: number
  branches: RetentionBranch[]
  removing: string[]
}

export interface SyncResult {
  action: 'sync'
  dry_run: boolean
  nodes: string[]
  unreachable: string[]
  /** Etiquetas que NO se reparten porque la retención va a borrarlas. */
  skipped_by_retention: number
  missing_count: number
  mismatch_count: number
  missing: string[]
  copied: string[]
  failed: string[]
  at: number
  error?: string
}

export interface RetentionResult {
  action: 'retention'
  dry_run: boolean
  enabled?: boolean
  keep_last: number
  protected: string[]
  blocked?: boolean
  would_remove?: number
  removed?: string[]
  failed?: string[]
  candidates?: RetentionCandidate[]
  reason?: string
  note?: string
  error?: string
}

export interface WindowResult {
  action: 'window'
  dry_run: boolean
  ok?: boolean
  at: number
  steps?: Record<string, unknown>
  progress?: WindowProgress
  coordinator?: string
  nodes?: string[]
  trigger?: 'manual' | 'scheduled'
  history?: MaintenanceHistorySummary
  history_errors?: string[]
  error?: string
}

export type WindowStepStatus = 'pending' | 'running' | 'completed' | 'skipped' | 'failed' | 'blocked'

export interface WindowStepProgress {
  id: 'retention' | 'gc' | 'sync'
  number: number
  label: string
  status: WindowStepStatus
  detail: string
  started_at: number | null
  finished_at: number | null
  nodes?: WindowNodeProgress[]
}

export interface WindowNodeProgress {
  name: string
  status: WindowStepStatus
  detail: string
  freed_bytes: number
}

export interface WindowProgress {
  running: boolean
  status: 'idle' | 'running' | 'completed' | 'failed'
  current: WindowStepProgress['id'] | null
  message: string
  started_at: number | null
  finished_at: number | null
  steps: WindowStepProgress[]
}

export interface MaintenanceHistorySummary {
  schema: number
  run_id: string
  dry_run: boolean
  status: 'completed' | 'failed'
  ok: boolean
  trigger: 'manual' | 'scheduled'
  coordinator: string
  nodes: string[]
  started_at: number
  finished_at: number
  duration_seconds: number
  message: string
  retention: { status: WindowStepStatus; count: number }
  gc: { status: WindowStepStatus; freed_bytes: number; registry_back: boolean }
  sync: {
    status: WindowStepStatus
    copied: number
    missing_count: number
    mismatch_count: number
    verified: boolean
  }
}

export interface Maintenance {
  enabled: boolean
  node: string
  keep_last: number
  protected_tags: string[]
  in_progress: boolean
  /** "03:30" en hora local del contenedor, o null si no hay ventana programada */
  window_hour: string | null
  window_gc: boolean
  window_next: number | null
  timezone: string
  peers: string[]
  cluster_mode: boolean
  cluster_nodes: string[]
  cluster_ready: boolean
  cluster_error: string | null
  coordinator: string
  coordinator_source: 'interface' | 'environment' | 'default'
  is_coordinator: boolean
  coordinator_reachable: boolean
  window_progress: WindowProgress
  history: {
    coordinator: string | null
    last_run: MaintenanceHistorySummary | null
    last_preview: MaintenanceHistorySummary | null
  }
  last: { gc?: GcResult; retention?: RetentionResult; sync?: SyncResult; window?: WindowResult }
}

export interface Snapshot {
  node: string
  registry: string
  generated_at: number
  took_ms: number
  disk_available: boolean
  summary: Summary
  projects: Project[]
  drift: Drift
  maintenance: Maintenance
}

export async function fetchAll(refresh = false): Promise<Snapshot> {
  const res = await fetch(`api/all${refresh ? '?refresh=1' : ''}`, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`El servicio respondió ${res.status}`)
  return res.json()
}

export async function fetchMaintenance(): Promise<Maintenance> {
  const res = await fetch('api/maintenance', { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`El servicio respondió ${res.status}`)
  return res.json()
}

/** Lo destructivo va por POST y solo borra de verdad con confirm=1. Sin él, el
 *  servidor simula y devuelve lo que haría. */
export type Tarea = 'gc' | 'retention' | 'sync' | 'window'

export async function runMaintenance(
  what: Tarea,
  confirm: boolean,
  csrfToken: string,
): Promise<GcResult | RetentionResult | SyncResult | WindowResult> {
  const res = await fetch(`api/maintenance/${what}${confirm ? '?confirm=1' : ''}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'X-Registry-Maintenance': '1',
      'X-CSRF-Token': csrfToken,
    },
  })
  if (!res.ok) throw new Error(`El servicio respondió ${res.status}`)
  return res.json()
}

export async function setMaintenanceCoordinator(coordinator: string, csrfToken: string): Promise<{
  ok: boolean
  changed: boolean
  coordinator: string
  previous_coordinator?: string
}> {
  const res = await fetch('api/maintenance/coordinator', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-Registry-Maintenance': '1',
      'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify({ coordinator }),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok || !body.ok) {
    throw new Error(body.error || `El servicio respondió ${res.status}`)
  }
  return body
}

export function formatBytes(n: number): string {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), u.length - 1)
  const v = n / Math.pow(1024, i)
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`
}

export function formatWhen(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString('es-ES', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}
