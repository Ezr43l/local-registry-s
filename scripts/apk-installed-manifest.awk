BEGIN {
    RS = ""
    FS = "\n"
    OFS = "\t"
    if (platform == "") {
        platform = "runtime"
    }
    print "platform", "package", "version", "architecture", "license", \
        "origin", "aports_commit", "upstream_url"
}

{
    package = ""
    version = ""
    architecture = ""
    license = ""
    origin = ""
    commit = ""
    upstream_url = ""

    for (line_number = 1; line_number <= NF; line_number++) {
        line = $line_number
        prefix = substr(line, 1, 2)
        value = substr(line, 3)
        if (prefix == "P:") package = value
        else if (prefix == "V:") version = value
        else if (prefix == "A:") architecture = value
        else if (prefix == "L:") license = value
        else if (prefix == "o:") origin = value
        else if (prefix == "c:") commit = value
        else if (prefix == "U:") upstream_url = value
    }

    normalized_license = toupper(license)
    # Familias recíprocas habituales en SPDX. Los límites evitan falsos
    # positivos como confundir una palabra que sólo contenga esas siglas.
    is_copyleft = normalized_license ~ \
        /(^|[^A-Z0-9])(AGPL|GPL|LGPL|MPL|EPL|EUPL|CDDL|CPL|OSL|CECILL|SSPL|RPL|CPAL|QPL|SISSL|NPL|IPL|APSL|CATOSL)([^A-Z0-9]|$)/
    if (package != "" && (!copyleft || is_copyleft)) {
        print platform, package, version, architecture, license, origin, commit, \
            upstream_url
    }
}
