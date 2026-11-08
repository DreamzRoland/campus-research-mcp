#!/bin/bash
set -eu
if [ "$(id -u)" != 0 ]; then
  echo 'Run: sudo bash /opt/campus-scopus/finish_setup.sh'
  exit 1
fi
run_check() {
  systemd-run --quiet --wait --pipe --collect \
    -p User=campus-scopus -p Group=campus-scopus \
    -p WorkingDirectory=/opt/campus-scopus \
    -p Environment=HOME=/var/lib/campus-scopus \
    -p Environment=PYTHONDONTWRITEBYTECODE=1 \
    -p LoadCredential=elsevier.json:/etc/campus-scopus/secrets.json \
    /var/lib/campus-scopus/.venv/bin/python "$@"
}
run_check /opt/campus-scopus/local_test.py
run_check /opt/campus-scopus/probe_api.py || true
python3 /opt/campus-scopus/configure_tunnel.py
systemctl enable --now campus-research-worker.service
systemd-run --quiet --wait --pipe --collect \
  -p User=campus-scopus -p Group=campus-scopus \
  -p Environment=HOME=/var/lib/campus-scopus \
  -p Environment=HTTPS_PROXY=http://127.0.0.1:17890 \
  -p Environment=HTTP_PROXY=http://127.0.0.1:17890 \
  -p Environment=NO_PROXY=localhost,127.0.0.1,api.elsevier.com,.elsevier.com \
  -p LoadCredential=elsevier.json:/etc/campus-scopus/secrets.json \
  /usr/bin/python3 /opt/campus-scopus/run_tunnel.py --doctor
curl --noproxy '*' --fail --silent --show-error --max-time 10 http://127.0.0.1:8088/readyz
