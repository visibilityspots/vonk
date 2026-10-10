# CSI volume for the vonk SQLite DB. Create /volume4/nomad/data/vonk/ on the NAS first
# and make it writable for the container user (UID 1000), or rely on mountPermissions.
# Register with: nomad volume register nomad/vonk-volume.hcl   (as for the other nfs-*.hcl)
type = "csi"
id = "vonk"
external_id = "vonk"
name = "vonk"
plugin_id = "nfs"
namespace = "services"

capability {
  access_mode = "single-node-writer"
  attachment_mode = "file-system"
}

context {
  server = "172.20.20.50"
  share = "/volume4/nomad/data/vonk/"
  mountPermissions = "0"
}

mount_options {
  fs_type = "nfs"
}
