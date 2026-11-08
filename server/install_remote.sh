#!/bin/bash
# Run on Ubuntu/Debian from the extracted package: sudo bash install_remote.sh
set -eu
cd -- "$(dirname -- "$0")"
test "$(id -u)" = 0 || { echo 'Run with sudo.'; exit 1; }
if ! id campus-scopus >/dev/null 2>&1; then
  useradd --system --user-group --create-home --home-dir /var/lib/campus-scopus --shell /usr/sbin/nologin campus-scopus
fi
install -d -m 0755 -o root -g root /opt/campus-scopus
install -d -m 0700 -o root -g root /etc/campus-scopus
if ! dpkg-query -W -f='${Status}' python3.12-venv 2>/dev/null | grep -q 'install ok installed'; then
  apt-get install -y python3.12-venv
fi
runuser -u campus-scopus -- python3 -m venv /var/lib/campus-scopus/.venv
runuser -u campus-scopus -- env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  /var/lib/campus-scopus/.venv/bin/pip install --index-url "${CAMPUS_PYPI_INDEX:-https://pypi.org/simple}" -r requirements.lock.txt
install -m 0644 server.py article_downloads.py article_bundle.py research_sources.py springer_bundle.py springer_pdf.py crossref_cambridge.py wiley_downloads.py aip_downloads.py aip_jobs.py publisher_downloads.py publisher_jobs.py native_file_output.py file_delivery.py export_article_stream.py download_widget.html probe_api.py verify_sciencedirect.py local_test.py write_secrets.py write_research_secrets.py write_wiley_secret.py configure_tunnel.py run_tunnel.py /opt/campus-scopus/
install -m 0644 ai_search.py /opt/campus-scopus/
install -d -m 0755 /opt/campus-scopus/retrieval
install -m 0644 retrieval/*.py /opt/campus-scopus/retrieval/
install -m 0644 export_research_file.py write_retrieval_secrets.py /opt/campus-scopus/
install -m 0644 campus-research-worker.service /etc/systemd/system/
install -m 0755 finish_setup.sh /opt/campus-scopus/
install -m 0644 campus-scopus-tunnel.service /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/campus-scopus-tunnel.service
systemctl enable campus-scopus-tunnel.service
if test -f /etc/campus-scopus/proxy.yaml && test -x /usr/local/bin/mihomo; then
  install -d -m 0700 -o campus-scopus -g campus-scopus /var/lib/campus-scopus/proxy
  install -m 0644 campus-scopus-proxy.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now campus-scopus-proxy.service
fi
runuser -u campus-scopus -- env PYTHONDONTWRITEBYTECODE=1 /var/lib/campus-scopus/.venv/bin/python /opt/campus-scopus/local_test.py
echo 'Install official tunnel-client separately if needed. Enter credentials using:'
echo 'sudo python3 /opt/campus-scopus/write_secrets.py'
echo 'Then run: sudo bash /opt/campus-scopus/finish_setup.sh'
