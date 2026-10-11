# vonk-poll: stateless periodic batch run of the same image as vonk.hcl. It talks to the
# service over its internal API and always reports back (/api/poll-report), also on failure.
# Vault kv/data/services/vonk-poll keys (the nomad-workloads policy grants kv/data/<namespace>/<job>) used: api_token, anthropic_api_key, takken
# (comma separated tak names, see src/vonk/models.py). Every secret is rendered with
# toJSON, so quotes or newlines in a value cannot break the env file; a missing key
# renders as "" (counts as missing) instead of the string null.
job "vonk-poll" {
  region      = "global"
  datacenters = ["garage"]
  namespace   = "services"
  type        = "batch"

  constraint {
    attribute = "${attr.cpu.arch}"
    operator  = "regexp"
    value     = "amd64|arm64"
  }

  periodic {
    crons            = ["0 */6 * * *"]
    time_zone        = "Europe/Brussels"
    prohibit_overlap = true
  }

  group "vonk-poll" {
    vault {
      # token renewal must not bounce the task; secrets arrive via template
      change_mode = "noop"
    }

    restart {
      attempts = 1
      delay    = "15s"
      interval = "60s"
      mode     = "fail"
    }

    task "vonk-poll" {
      driver = "podman"

      template {
        data = <<EOF2
TZ = "Europe/Brussels"
# no /api suffix: the poll command appends it
VONK_API_URL = "https://vonk.visibilityspots.net"
{{with secret "kv/data/services/vonk-poll"}}
VONK_API_TOKEN = {{with .Data.data.api_token}}{{. | toJSON}}{{else}}""{{end}}
ANTHROPIC_API_KEY = {{with .Data.data.anthropic_api_key}}{{. | toJSON}}{{else}}""{{end}}
TAKKEN = {{with .Data.data.takken}}{{. | toJSON}}{{else}}""{{end}}
{{end}}
EOF2
        destination = "local/env"
        env         = true
      }

      config {
        image = "registry.visibilityspots.net/visibilityspots/vonk:v0.2.2"
        args  = ["poll"]
        logging {
          driver = "journald"
          options = [{
            tag = "VONK-POLL"
          }]
        }
      }

      resources {
        memory = 256
        cpu    = 200
      }
    }
  }
}
