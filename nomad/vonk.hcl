# vonk service: owns the SQLite DB, the approve page, the internal API and every
# CalDAV write. Same image as vonk-poll.hcl. count must stay 1 (single SQLite writer).
#
# Image tag: set the released semver tag at deploy time (v0.1.0 is the first).
# Vault kv/data/services/vonk keys: baikal_url, baikal_user, baikal_password,
# baikal_gezin_uri (the calendar collection name under calendars/<user>/, for a share its
# UUID; baikal_calendar is only the display name and is NOT used), gotify_url,
# gotify_token, api_token, child_names (JSON object tak -> name stored as a JSON *string*).
# Every secret is rendered with toJSON, so quotes or newlines in a value cannot break the
# env file; a missing key renders as "" (counts as missing) instead of the string null. The volume comes from vonk-volume.hcl.
job "vonk" {
  region      = "global"
  datacenters = ["garage"]
  namespace   = "services"
  type        = "service"

  constraint {
    attribute = "${attr.cpu.arch}"
    operator  = "regexp"
    value     = "amd64|arm64"
  }

  group "vonk" {
    count = 1

    vault {
      # token renewal must not bounce the task; secrets arrive via template
      change_mode = "noop"
    }

    update {
      auto_revert = true
    }

    restart {
      attempts = 3
      interval = "30m"
      delay    = "15s"
      mode     = "fail"
    }

    volume "vonk" {
      type            = "csi"
      source          = "vonk"
      access_mode     = "single-node-writer"
      attachment_mode = "file-system"
    }

    network {
      port "vonk_ui" {
        to = 8080
      }
    }

    service {
      name = "vonk"
      port = "vonk_ui"
      tags = [
        "metrics",
        "traefik.enable=true",
        "traefik.http.routers.vonk.rule=Host(`vonk.visibilityspots.net`)",
        # internal https entrypoint only: the approve page and /api must never be public
        "traefik.http.routers.vonk.entrypoints=https",
        "traefik.http.routers.vonk.tls=true",
      ]

      check {
        type     = "http"
        port     = "vonk_ui"
        path     = "/health"
        interval = "20s"
        timeout  = "5s"
      }
    }

    task "vonk" {
      driver = "podman"

      template {
        data = <<EOF2
TZ = "Europe/Brussels"
VONK_PUBLIC_URL = "https://vonk.visibilityspots.net"
VONK_DB_PATH = "/data/vonk.db"
{{with secret "kv/data/services/vonk"}}
BAIKAL_URL = {{with .Data.data.baikal_url}}{{. | toJSON}}{{else}}""{{end}}
BAIKAL_USER = {{with .Data.data.baikal_user}}{{. | toJSON}}{{else}}""{{end}}
BAIKAL_PASSWORD = {{with .Data.data.baikal_password}}{{. | toJSON}}{{else}}""{{end}}
BAIKAL_CALENDAR = {{with .Data.data.baikal_gezin_uri}}{{. | toJSON}}{{else}}""{{end}}
GOTIFY_URL = {{with .Data.data.gotify_url}}{{. | toJSON}}{{else}}""{{end}}
GOTIFY_TOKEN = {{with .Data.data.gotify_token}}{{. | toJSON}}{{else}}""{{end}}
VONK_API_TOKEN = {{with .Data.data.api_token}}{{. | toJSON}}{{else}}""{{end}}
CHILD_NAMES = {{with .Data.data.child_names}}{{. | toJSON}}{{else}}""{{end}}
{{end}}
EOF2
        destination = "local/env"
        env         = true
      }

      config {
        image = "registry.visibilityspots.net/visibilityspots/vonk:v0.2.2"
        args  = ["serve"]
        ports = ["vonk_ui"]
        logging {
          driver = "journald"
          options = [{
            tag = "VONK"
          }]
        }
      }

      volume_mount {
        volume      = "vonk"
        destination = "/data"
      }

      resources {
        memory = 256
        cpu    = 200
      }
    }
  }
}
