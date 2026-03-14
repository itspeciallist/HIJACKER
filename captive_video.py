#!/usr/bin/env python3
"""
Captive Portal Video Server — Educational Use Only
Usage:
    sudo python3 captive_video.py --video testvideo.mp4
    sudo python3 captive_video.py --video testvideo.mp4 --ssid "FreeWifi" --pass "12345678"
    sudo python3 captive_video.py --video testvideo.mp4 --iface wlan0
"""

import os, sys, time, signal, argparse
import threading, subprocess, http.server, urllib.parse
from pathlib import Path

R="\033[91m"; G="\033[92m"; Y="\033[93m"
C="\033[96m"; B="\033[1m";  X="\033[0m"
def info(m): print(f"{C}[*]{X} {m}")
def ok(m):   print(f"{G}[✓]{X} {m}")
def warn(m): print(f"{Y}[!]{X} {m}")
def err(m):  print(f"{R}[✗]{X} {m}")
def step(n,t): print(f"\n{B}{C}{'═'*52}\n  STEP {n}  —  {t}\n{'═'*52}{X}\n")

HOTSPOT_CON  = "captive-hotspot"
GATEWAY_IP   = "10.0.0.1"
HTTP_PORT    = 80
VIDEO_PATH   = None
dnsmasq_proc = None

MIME_TYPES = {
    ".mp4":"video/mp4",".webm":"video/webm",
    ".ogg":"video/ogg",".mkv":"video/x-matroska",
    ".avi":"video/x-msvideo",".mov":"video/quicktime",
}

# ── Step 1: Dependencies ──────────────────────────────────
def install_deps():
    step(1, "Installing Dependencies")
    pkgs = ["dnsmasq","iptables","network-manager","iw","hostapd"]
    missing = [p for p in pkgs if
               "install ok installed" not in
               subprocess.run(["dpkg","-s",p],capture_output=True,text=True).stdout]
    if not missing:
        ok("All dependencies already installed."); return
    warn(f"Installing: {', '.join(missing)}")
    subprocess.run(["apt","update","-qq"], check=True)
    subprocess.run(["apt","install","-y","-qq"]+missing, check=True)
    ok("Done.")

# ── Interface diagnosis ───────────────────────────────────
def diagnose_interfaces():
    """Show all WiFi interfaces and their modes."""
    info("Scanning WiFi interfaces...")
    r = subprocess.run(["iw","dev"], capture_output=True, text=True)
    print(r.stdout)
    r2 = subprocess.run(["nmcli","device","status"], capture_output=True, text=True)
    print(r2.stdout)

def get_iface_info():
    """Return dict of {iface: mode} for all WiFi interfaces."""
    r = subprocess.run(["iw","dev"], capture_output=True, text=True)
    ifaces = {}
    current = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Interface"):
            current = line.split()[1]
            ifaces[current] = {"mode": "unknown", "skip": False}
        elif line.startswith("type") and current:
            ifaces[current]["mode"] = line.split()[1]
        if current and any(s in current for s in ["wfphshr","mon","uap","ap0"]):
            ifaces[current]["skip"] = True
    return ifaces

def pick_ap_iface(preferred=None):
    """Pick best interface for AP mode."""
    ifaces = get_iface_info()
    info("Detected interfaces:")
    for name, info_d in ifaces.items():
        flag = " ← wifiphisher/skip" if info_d["skip"] else ""
        print(f"    {B}{name}{X}  mode={info_d['mode']}{flag}")

    if preferred:
        if preferred in ifaces:
            ok(f"Using specified interface: {B}{preferred}{X}")
            return preferred
        else:
            warn(f"Specified interface {preferred} not found, auto-detecting...")

    # Prefer managed mode interfaces (not already in AP/monitor)
    for name, d in ifaces.items():
        if not d["skip"] and d["mode"] == "managed":
            ok(f"Selected: {B}{name}{X} (managed mode, good for AP)")
            return name

    # Fallback: any non-skip interface
    for name, d in ifaces.items():
        if not d["skip"]:
            ok(f"Selected: {B}{name}{X}")
            return name

    warn("No suitable interface found, defaulting to wlan0")
    return "wlan0"

def get_iface_ip(iface):
    for _ in range(12):
        r = subprocess.run(["ip","-4","addr","show",iface],capture_output=True,text=True)
        for line in r.stdout.splitlines():
            if "inet " in line:
                ip = line.strip().split()[1].split("/")[0]
                # Must not be a home router IP range we don't control
                return ip
        time.sleep(1)
    return GATEWAY_IP

# ── Step 2: Hotspot via hostapd + manual IP ───────────────
HOSTAPD_CONF = "/tmp/captive_hostapd.conf"
hostapd_proc = None

def setup_hotspot_hostapd(iface, ssid, password):
    """Use hostapd directly — most reliable AP method."""
    global hostapd_proc
    info(f"Setting up AP with hostapd on {B}{iface}{X}")

    # Disconnect interface from NetworkManager so we control it
    subprocess.run(["nmcli","device","disconnect", iface], capture_output=True)
    subprocess.run(["nmcli","device","set", iface, "managed","no"], capture_output=True)
    time.sleep(1)

    # Set interface up
    subprocess.run(["ip","link","set", iface,"up"], capture_output=True)

    # Assign gateway IP
    subprocess.run(["ip","addr","flush","dev", iface], capture_output=True)
    subprocess.run(["ip","addr","add",f"{GATEWAY_IP}/24","dev",iface], check=True, capture_output=True)

    open_net = not password

    conf = f"""interface={iface}
driver=nl80211
ssid={ssid}
hw_mode=g
channel=6
macaddr_acl=0
ignore_broadcast_ssid=0
"""
    if not open_net:
        conf += f"""auth_algs=1
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
"""

    with open(HOSTAPD_CONF,"w") as f:
        f.write(conf)

    hostapd_proc = subprocess.Popen(
        ["hostapd", HOSTAPD_CONF],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(3)

    if hostapd_proc.poll() is not None:
        out, errbytes = hostapd_proc.communicate()
        raise RuntimeError(f"hostapd failed:\n{errbytes.decode()}")

    ok(f"hostapd running  SSID={B}{ssid}{X}  IP={B}{GATEWAY_IP}{X}")
    return GATEWAY_IP

def setup_hotspot_nmcli(iface, ssid, password):
    """Fallback: nmcli hotspot."""
    subprocess.run(["nmcli","con","delete", HOTSPOT_CON], capture_output=True)
    subprocess.run(["nmcli","device","set", iface, "managed","yes"], capture_output=True)
    time.sleep(1)

    cmd = ["nmcli","device","wifi","hotspot",
           "ifname", iface, "con-name", HOTSPOT_CON, "ssid", ssid,
           "password", password if password else "changeme1"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"nmcli hotspot failed: {r.stderr}")

    time.sleep(4)
    gw = get_iface_ip(iface)
    ok(f"nmcli hotspot up  Gateway: {B}{gw}{X}")
    return gw

def setup_hotspot(iface, ssid, password):
    step(2, "Setting Up Hotspot")

    # Try hostapd first (most reliable)
    try:
        gw = setup_hotspot_hostapd(iface, ssid, password)
        return gw
    except Exception as e:
        warn(f"hostapd method failed: {e}")
        warn("Falling back to nmcli...")

    # Fallback to nmcli
    gw = setup_hotspot_nmcli(iface, ssid, password)
    return gw

# ── Step 3: DHCP + DNS Hijack ─────────────────────────────
DNSMASQ_CONF = "/tmp/captive_dnsmasq.conf"

def start_dns_hijack(iface, gw):
    step(3, "DHCP + DNS Hijack")
    global dnsmasq_proc
    subprocess.run(["systemctl","stop","dnsmasq"], capture_output=True)
    subprocess.run(["pkill","-9","-f","dnsmasq"],  capture_output=True)
    time.sleep(1)

    subnet = gw.rsplit(".",1)[0]
    with open(DNSMASQ_CONF,"w") as f:
        f.write(f"""
interface={iface}
bind-interfaces
listen-address={gw}
dhcp-range={subnet}.10,{subnet}.100,255.255.255.0,12h
dhcp-option=3,{gw}
dhcp-option=6,{gw}
no-resolv
# Redirect ALL domains to us (captive portal)
address=/#/{gw}
""")

    dnsmasq_proc = subprocess.Popen(
        ["dnsmasq","--conf-file="+DNSMASQ_CONF,"--no-daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)

    # iptables: redirect DNS + HTTP
    subprocess.run(["iptables","-t","nat","-F"], capture_output=True)
    subprocess.run([
        "iptables","-t","nat","-A","PREROUTING",
        "-i",iface,"-p","udp","--dport","53",
        "-j","DNAT","--to-destination",f"{gw}:53"
    ], capture_output=True)
    subprocess.run([
        "iptables","-t","nat","-A","PREROUTING",
        "-i",iface,"-p","tcp","--dport","80",
        "-j","DNAT","--to-destination",f"{gw}:{HTTP_PORT}"
    ], capture_output=True)
    subprocess.run(["sysctl","-w","net.ipv4.ip_forward=1"], capture_output=True)
    ok(f"DHCP serving  {subnet}.10 — {subnet}.100")
    ok("DNS hijack active — all domains → us")

# ── Step 4: HTTP Captive Portal ───────────────────────────
HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Video</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;background:#000;overflow:hidden}
video{width:100vw;height:100vh;object-fit:contain;display:block}
#tap{position:fixed;top:0;left:0;width:100%;height:100%;
     display:flex;align-items:center;justify-content:center;
     cursor:pointer;z-index:10;background:rgba(0,0,0,0.25)}
#tap span{color:#fff;font-size:80px;filter:drop-shadow(0 0 20px #fff)}
</style>
</head><body>
<video id="v" src="/video" playsinline webkit-playsinline
       x5-playsinline preload="auto" loop></video>
<div id="tap"><span>&#9654;</span></div>
<script>
var v=document.getElementById('v'),tap=document.getElementById('tap');
function goFS(){
  if(document.documentElement.requestFullscreen) document.documentElement.requestFullscreen();
  else if(v.webkitEnterFullscreen) v.webkitEnterFullscreen();
  else if(v.requestFullscreen) v.requestFullscreen();
}
function play(){
  tap.style.display='none';
  v.play().then(function(){setTimeout(goFS,400);}).catch(function(){});
}
v.play().then(function(){tap.style.display='none';setTimeout(goFS,600);})
        .catch(function(){tap.style.display='flex';});
tap.addEventListener('click',play);
tap.addEventListener('touchend',function(e){e.preventDefault();play();});
</script>
</body></html>"""

CAPTIVE_PATHS = {
    "/generate_204","/gen_204","/mobile/status.php",
    "/hotspot-detect.html","/library/test/success.html",
    "/connecttest.txt","/ncsi.txt","/success.txt","/redirect",
}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        info(f"HTTP {self.address_string()}  {fmt%args}")
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/video":
            self._serve_video(); return
        if path in CAPTIVE_PATHS:
            body = (f'<html><head>'
                    f'<meta http-equiv="refresh" content="0;url=http://{GATEWAY_IP}/">'
                    f'</head></html>').encode()
            self.send_response(302)
            self.send_header("Location", f"http://{GATEWAY_IP}/")
            self.send_header("Content-Type","text/html")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html;charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-cache")
        self.end_headers(); self.wfile.write(body)
    def _serve_video(self):
        if not VIDEO_PATH or not os.path.isfile(VIDEO_PATH):
            self.send_error(404); return
        mime  = MIME_TYPES.get(Path(VIDEO_PATH).suffix.lower(),"video/mp4")
        fsize = os.path.getsize(VIDEO_PATH)
        rng   = self.headers.get("Range")
        if rng:
            p=rng.replace("bytes=","").split("-")
            s=int(p[0]) if p[0] else 0
            e=int(p[1]) if p[1] else fsize-1
            e=min(e,fsize-1); ln=e-s+1
            self.send_response(206)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Range",f"bytes {s}-{e}/{fsize}")
            self.send_header("Content-Length",str(ln))
            self.send_header("Accept-Ranges","bytes")
            self.end_headers()
            with open(VIDEO_PATH,"rb") as f:
                f.seek(s); rem=ln
                while rem>0:
                    c=f.read(min(65536,rem))
                    if not c: break
                    self.wfile.write(c); rem-=len(c)
        else:
            self.send_response(200)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(fsize))
            self.send_header("Accept-Ranges","bytes")
            self.end_headers()
            with open(VIDEO_PATH,"rb") as f:
                while True:
                    c=f.read(65536)
                    if not c: break
                    self.wfile.write(c)
    def do_HEAD(self): self.do_GET()

def start_http(gw):
    step(4, "Starting HTTP Captive Portal")
    srv = http.server.HTTPServer(("0.0.0.0",HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok(f"HTTP on :{HTTP_PORT}  →  http://{gw}/")
    return srv

# ── Cleanup ───────────────────────────────────────────────
def cleanup(iface="wlan0"):
    print(f"\n{Y}[*] Shutting down...{X}")
    global hostapd_proc, dnsmasq_proc
    if hostapd_proc:
        try: hostapd_proc.terminate()
        except: pass
    if dnsmasq_proc:
        try: dnsmasq_proc.terminate()
        except: pass
    subprocess.run(["iptables","-t","nat","-F"], capture_output=True)
    subprocess.run(["nmcli","con","down",  HOTSPOT_CON], capture_output=True)
    subprocess.run(["nmcli","con","delete", HOTSPOT_CON], capture_output=True)
    subprocess.run(["nmcli","device","set", iface,"managed","yes"], capture_output=True)
    subprocess.run(["systemctl","start","dnsmasq"], capture_output=True)
    for f in [DNSMASQ_CONF, HOSTAPD_CONF]:
        if os.path.exists(f): os.remove(f)
    ok("Cleaned up.")

# ── Main ──────────────────────────────────────────────────
def main():
    global VIDEO_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--video",      required=True)
    ap.add_argument("--ssid",       default="FreeWifi")
    ap.add_argument("--pass",       dest="password", default="")
    ap.add_argument("--iface",      default=None,
                    help="Force a specific WiFi interface e.g. --iface wlan0")
    ap.add_argument("--no-hotspot", action="store_true")
    ap.add_argument("--diagnose",   action="store_true",
                    help="Show interface info and exit")
    args = ap.parse_args()

    if os.geteuid() != 0:
        err("Run as root:  sudo python3 captive_video.py --video file.mp4")
        sys.exit(1)

    if args.diagnose:
        diagnose_interfaces()
        sys.exit(0)

    if not os.path.isfile(args.video):
        err(f"Video not found: {args.video}")
        err(f"Files here: {[f for f in os.listdir('.') if os.path.isfile(f)]}")
        sys.exit(1)

    VIDEO_PATH = os.path.abspath(args.video)
    size_mb    = os.path.getsize(VIDEO_PATH)/1024/1024

    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   CAPTIVE PORTAL VIDEO SERVER            ║")
    print(f"  ║   Educational Use Only                   ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    info(f"Video : {B}{args.video}{X} ({size_mb:.1f} MB)")
    info(f"SSID  : {B}{args.ssid}{X}")
    info(f"Pass  : {B}{'(open)' if not args.password else args.password}{X}")

    iface = pick_ap_iface(args.iface)

    try:
        install_deps()

        if not args.no_hotspot:
            gw = setup_hotspot(iface, args.ssid, args.password)
        else:
            gw = GATEWAY_IP
            warn("Skipping hotspot (--no-hotspot).")

        start_dns_hijack(iface, gw)
        start_http(gw)

        print(f"""
{B}{G}╔══════════════════════════════════════════════════════╗
║           ✅  CAPTIVE PORTAL IS LIVE!                ║
╠══════════════════════════════════════════════════════╣
║  {X}Interface  :  {B}{iface:<40}{G}║
║  {X}WiFi SSID  :  {B}{args.ssid:<40}{G}║
║  {X}Password   :  {B}{(args.password or '(open)'):<40}{G}║
║  {X}Gateway    :  {B}{gw:<40}{G}║
╠══════════════════════════════════════════════════════╣
║  {X}  1. Connect phone to WiFi: {B}{args.ssid}{X}              {G}║
║  {X}  2. Android detects captive portal              {G}║
║  {X}  3. Browser popup opens automatically  📲       {G}║
║  {X}  4. Video plays full screen  🎬                 {G}║
╚══════════════════════════════════════════════════════╝{X}
{Y}Ctrl+C to stop.{X}
""")
        seen = set()
        def shutdown(s,f): cleanup(iface); sys.exit(0)
        signal.signal(signal.SIGINT,  shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        while True:
            time.sleep(5)
            leases = "/var/lib/misc/dnsmasq.leases"
            if os.path.exists(leases):
                for line in open(leases):
                    p = line.strip().split()
                    if len(p)>=4 and p[1] not in seen:
                        seen.add(p[1])
                        ok(f"📱 Device joined!  IP={B}{p[2]}{X}  "
                           f"Name={B}{p[3]}{X}  MAC={p[1]}")

    except KeyboardInterrupt:
        cleanup(iface)
    except Exception as e:
        err(str(e))
        import traceback; traceback.print_exc()
        cleanup(iface); sys.exit(1)

if __name__ == "__main__":
    main()
