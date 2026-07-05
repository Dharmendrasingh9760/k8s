# Service-Level Monitoring Dashboard & Alert Escalation System

An open-source DevOps monitoring stack that tracks **CPU and RAM usage at the
service (container) level**, visualizes it on a Grafana dashboard, and
escalates alerts through a two-tier (Warning → Critical) routing pipeline
using Prometheus Alertmanager.

---

## 1. Architecture

```
                       ┌─────────────────┐
                       │  demo-service-a │
                       │  demo-service-b │  <-- the "services" being monitored
                       └────────┬────────┘
                                │ container stats
                       ┌────────▼────────┐        ┌──────────────┐
                       │     cAdvisor     │        │ node-exporter│  <-- host-level
                       │ (per-container   │        │ (host CPU/RAM│      CPU/RAM
                       │  CPU/RAM metrics)│        │  metrics)    │
                       └────────┬────────┘        └──────┬───────┘
                                │        scrape (15s)     │
                                └───────────┬─────────────┘
                                            │
                                    ┌───────▼────────┐
                                    │   Prometheus    │  <-- stores metrics,
                                    │ (metrics store  │      evaluates alert
                                    │ + rule engine)  │      rules every 15s
                                    └───┬────────┬────┘
                                        │        │
                       alerts firing    │        │  PromQL queries
                                        │        │
                              ┌─────────▼──┐  ┌──▼────────┐
                              │Alertmanager │  │  Grafana  │  <-- dashboard
                              │ (routing,   │  │(dashboard │      (CPU/RAM
                              │ grouping,   │  │ + alerts  │       per service,
                              │ escalation) │  │  panel)   │       gauges, table)
                              └──────┬──────┘  └───────────┘
                                     │
                     ┌───────────────┼────────────────┐
                     │                                │
             severity=warning                  severity=critical
                     │                                │
             ┌───────▼────────┐              ┌────────▼─────────┐
             │ Warning         │              │ Critical           │
             │ Notification    │              │ Escalation         │
             │ (team channel/  │              │ (on-call/incident  │
             │  webhook, hourly│              │  channel + email,  │
             │  repeat)        │              │  repeats every 15m)│
             └─────────────────┘              └────────────────────┘
```

**Escalation logic (in `alertmanager/alertmanager.yml`):** applies to
*all three* alert families — CPU, RAM/Disk, and **application error rate**
— since they all just set `severity: warning` or `severity: critical`
labels that the same routing tree matches on.

1. When CPU/RAM crosses the **warning** threshold (70% CPU / 75% RAM) for
   2 minutes, Prometheus fires a `severity=warning` alert. Alertmanager routes
   it to the team notification channel and re-notifies at most once an hour.
2. If usage keeps climbing past the **critical** threshold (90%) for 5
   minutes, a separate `severity=critical` alert fires. Alertmanager routes
   this to the **critical-escalation** receiver (on-call/incident channel +
   email) and re-notifies every **15 minutes** until it's resolved — modeling
   an incident that keeps escalating until someone acknowledges it.
3. **Error escalation** works the same way but on HTTP 5xx ratio instead of
   resource usage: >5% error rate for 2m = warning, >20% for 3m = critical,
   and a sudden burst of 20+ errors in 1 minute escalates immediately
   regardless of ratio (`ServiceErrorSpike`) — since a fast spike often means
   an outage, not a slow degradation.
4. An **inhibition rule** suppresses the warning notification for the same
   service/category once it has escalated to critical, so on-call isn't
   spammed with both.

---

## 2. Components used (all open-source / community edition)

| Component      | Purpose                                   |
|-----------------|-------------------------------------------|
| Prometheus      | Metrics collection & alert rule evaluation |
| Node Exporter   | Host-level CPU/RAM metrics                |
| cAdvisor        | Per-container (service-level) CPU/RAM metrics |
| Alertmanager    | Alert routing, grouping, escalation        |
| Grafana OSS     | Dashboard visualization                    |
| http-echo (webhook-logger) | Stand-in "incident channel" endpoint that logs every alert it receives (swap for a real Slack/PagerDuty webhook in production) |

---

## 2a. Deploying on a single Ubuntu EC2 instance (host-level CPU/RAM/Disk)

If you're monitoring the EC2 host itself (not multiple containerized
services), here's the exact path:

1. **Open ports in the EC2 Security Group** (inbound rules):
   - `3000` (Grafana) — restrict source to "My IP", don't open to `0.0.0.0/0`
   - `22` (SSH) — usually already open
   - You do **not** need to open 9090/9093/9100 publicly — Prometheus,
     Alertmanager and node-exporter only need to talk to each other inside
     the Docker network on the instance.

2. **Install Docker on the EC2 instance** — copy `scripts/install-docker-ec2.sh`
   onto the instance and run:
   ```bash
   chmod +x install-docker-ec2.sh
   ./install-docker-ec2.sh
   newgrp docker   # or log out/in
   ```

3. **Upload this whole project folder** to the EC2 instance, e.g.:
   ```bash
   scp -i your-key.pem -r devops-assignment ubuntu@<EC2_PUBLIC_IP>:~/
   ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>
   cd devops-assignment
   docker compose up -d
   ```

4. **Open Grafana** at `http://<EC2_PUBLIC_IP>:3000` (admin/admin) — the
   dashboard is already provisioned and will show host **CPU %, Memory %,
   and Disk %** panels (node-exporter metrics), because `node-exporter`
   mounts the EC2 host's `/`, `/proc`, `/sys` read-only from inside the
   container — so it reports the real EC2 host stats, not just the
   container's own.

   > `web-service-a` and `web-service-b` are a small real Flask app (see
   > `app/app.py`) — not just idle placeholders. They serve a health check,
   > a baseline-traffic endpoint, and on-demand `/load-cpu` and
   > `/load-memory` endpoints you can hit with `curl` to trigger alerts
   > without needing `stress-ng` at all (see section 5 below). If you
   > don't need this and only want the bare EC2 host's CPU/RAM/Disk, you
   > can remove `cadvisor`, `web-service-a`, and `web-service-b` from
   > `docker-compose.yml` — the host-level panels (from node-exporter)
   > work standalone.

5. **Test alerting** the same way as below (section 5), just SSH into the
   EC2 instance first, then run the `stress-ng` commands against whichever
   container/host you're targeting. For host-level CPU/RAM/Disk pressure
   specifically, you can also just run `stress-ng` directly on the EC2 host
   (outside Docker) — node-exporter will pick it up since it reads real host
   `/proc` stats:
   ```bash
   sudo apt-get install -y stress-ng
   stress-ng --cpu $(nproc) --timeout 360s        # CPU alert
   stress-ng --vm 1 --vm-bytes 80% --timeout 360s  # RAM alert
   fallocate -l 5G ~/bigfile.img                   # Disk alert (delete after: rm ~/bigfile.img)
   ```

---

## 2b. Deploying on a Kubernetes cluster

Uses the standard open-source **kube-prometheus-stack** Helm chart
(Prometheus Operator + node-exporter + kube-state-metrics + Alertmanager +
Grafana, all pre-integrated) instead of the Docker Compose stack. All the
manifests live in the `k8s/` folder.

### Prerequisites
- A running Kubernetes cluster (minikube, kind, EKS, kubeadm — any of them)
  with `kubectl` pointed at it (`kubectl get nodes` should work)
- `helm` v3 installed
- A container registry to push the demo app's image to (Docker Hub is
  fine), **or** if using minikube/kind, you can load the image directly
  without pushing anywhere (see below)

### Step 1 — Install kube-prometheus-stack via Helm
```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f k8s/helm-values.yaml
```
Wait for everything to come up:
```bash
kubectl get pods -n monitoring -w
```

### Step 2 — Build & make the demo app's image available to the cluster
```bash
cd app
docker build -t devops-demo-app:latest .

# Option A: minikube
minikube image load devops-demo-app:latest

# Option B: kind
kind load docker-image devops-demo-app:latest

# Option C: real cluster (EKS/managed) - push to a registry instead
docker tag devops-demo-app:latest <your-dockerhub-username>/devops-demo-app:latest
docker push <your-dockerhub-username>/devops-demo-app:latest
```
Then edit `k8s/app-deployment.yaml` and replace both occurrences of
`REPLACE_WITH_YOUR_IMAGE` with `devops-demo-app:latest` (minikube/kind) or
`<your-dockerhub-username>/devops-demo-app:latest` (real registry).

### Step 3 — Deploy the app, ServiceMonitors, alert rules, and webhook logger
```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/app-deployment.yaml
kubectl apply -f k8s/webhook-logger.yaml
kubectl apply -f k8s/servicemonitor.yaml
kubectl apply -f k8s/prometheusrule.yaml
```

### Step 4 — Access Grafana
```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```
Open `http://localhost:3000` (admin/admin, per `helm-values.yaml`). Import
`grafana/dashboards/service_monitoring.json` manually (Dashboards → Import →
Upload JSON) — Helm-deployed Grafana doesn't auto-load the same
provisioning folder used in the Docker Compose setup.

> On an EC2-hosted cluster, you can instead set
> `grafana.service.type: NodePort` in `k8s/helm-values.yaml` and open the
> EC2 instance's IP on the assigned NodePort (remember to open that port
> in the Security Group, restricted to your IP).

### Step 5 — Test alerting end-to-end
```bash
# Port-forward the app so you can curl it
kubectl port-forward -n devops-demo svc/web-service-a 5001:5000

# In another terminal:
curl "http://localhost:5001/load-cpu?seconds=360"        # CPU alert
curl "http://localhost:5001/load-memory?mb=200&seconds=360"  # RAM alert (keep under the 256Mi limit's danger zone but over 75%)
curl "http://localhost:5001/trigger-error?rate=50&seconds=180" && \
  for i in $(seq 1 100); do curl -s http://localhost:5001/work > /dev/null; sleep 1; done  # error-rate alert
```
Watch alerts progress warning → critical:
```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-alertmanager 9093:9093
# open http://localhost:9093

kubectl logs -n devops-demo -l app=webhook-logger -f   # see escalated alerts land here
```

### Notes specific to K8s
- CPU/RAM alert thresholds here are **relative to each container's resource
  `limits`** (set in `k8s/app-deployment.yaml`, 250m CPU / 256Mi RAM) rather
  than relative to 100% of a host core, since that's the standard way to
  reason about "is this service under pressure" in Kubernetes.
- Disk alerts remain node-level (via node-exporter, which kube-prometheus-stack
  installs as a DaemonSet automatically) — same as the EC2 host-level setup.
- `ServiceMonitor` + `PrometheusRule` are CRDs the Prometheus Operator
  watches and auto-loads — no manual `prometheus.yml` reload needed, unlike
  the Docker Compose version.

---

## 3. Prerequisites (Docker Compose path)

- Docker Engine ≥ 20.10
- Docker Compose ≥ v2
- ~2 GB of free RAM for the stack itself
- `stress-ng` (optional, only needed to *test* alerting by generating load)
- Ports free on the host: `3000, 8080, 8081, 9090, 9093, 9100`

---

## 4. Installation & Setup

```bash
# 1. Clone/copy this project folder, then from its root:
docker compose up -d

# 2. Confirm all containers are healthy
docker compose ps
```

Services will be available at:

| Service       | URL                          | Credentials      |
|---------------|-------------------------------|-------------------|
| Grafana       | http://localhost:3000         | admin / admin (change on first login) |
| Prometheus    | http://localhost:9090         | none |
| Alertmanager  | http://localhost:9093         | none |
| cAdvisor      | http://localhost:8080          | none |
| Webhook logger| http://localhost:8081          | none |

The Grafana dashboard **"Service Monitoring & Alerting Dashboard"** is
auto-provisioned — no manual import needed. It shows:
- Per-service CPU % and RAM % time series (color-coded by threshold)
- Host-level CPU/RAM gauges
- A live table of currently firing alerts
- A stat panel counting active alerts by severity

> **Note on the email receiver:** `alertmanager/alertmanager.yml` includes a
> placeholder SMTP config. Either fill in real SMTP credentials, or simply
> leave it — the webhook receiver (`webhook-logger`) will still work and is
> sufficient to demonstrate escalation end-to-end.

---

## 5. Running & Testing the Solution

### 5a. Confirm metrics are flowing
Open Prometheus targets page: http://localhost:9090/targets — all jobs
(`prometheus`, `node-exporter`, `cadvisor`, `alertmanager`) should show **UP**.

### 5b. Confirm the demo application is running
```bash
curl http://localhost:5001/          # web-service-a health check
curl http://localhost:5002/          # web-service-b health check
curl http://localhost:5001/work      # generates a bit of real baseline CPU traffic
```

### 5c. Trigger a CPU alert (no stress-ng needed — the app does it itself)
```bash
# Peg web-service-a's CPU for 6 minutes (long enough to cross both
# warning (2m) and critical (5m) thresholds)
curl "http://localhost:5001/load-cpu?seconds=360"
```
To stop early: `curl http://localhost:5001/stop`

### 5d. Watch the escalation happen
1. **~2 minutes in:** `ServiceHighCPUUsage` (warning) appears in Prometheus →
   Alerts, then in Alertmanager → http://localhost:9093, then a POST is logged
   by `webhook-logger` (`docker logs webhook-logger`).
2. **~5 minutes in (if load continues past 90%):** `ServiceCriticalCPUUsage`
   fires, routes to the `critical-escalation` receiver, and the warning
   notification is inhibited.
3. Watch the Grafana dashboard's CPU panel and "Active Alerts" table update
   live throughout, with `web-service-a` clearly spiking vs. `web-service-b`
   staying flat — a nice visual for the video.

### 5e. Trigger a RAM alert
```bash
curl "http://localhost:5001/load-memory?mb=400&seconds=360"
```

### 5f. Trigger a disk alert (on the EC2 host directly)
```bash
fallocate -l 5G ~/bigfile.img   # fills disk
# ... watch HostHighDiskUsage / HostCriticalDiskUsage fire ...
rm ~/bigfile.img                # cleans up, alert auto-resolves
```

---

### 5g. Trigger error escalation (the "Error Escalation" requirement)
```bash
# Start injecting ~50% HTTP 500 errors into web-service-a's /work endpoint for 3 minutes
curl "http://localhost:5001/trigger-error?rate=50&seconds=180"

# Now generate traffic so the error rate actually gets measured
for i in $(seq 1 100); do curl -s http://localhost:5001/work > /dev/null; sleep 1; done
```
Watch:
1. `ServiceHighErrorRate` (warning, >5% errors for 2m) fires first.
2. If error rate stays above 20%, `ServiceCriticalErrorRate` escalates it to
   the critical/on-call receiver.
3. `ServiceErrorSpike` fires immediately (within 1m) if more than 20 errors
   happen in a single minute — models a sudden outage that needs instant
   escalation, not just a slow-building trend.
4. The Grafana **"HTTP Error Rate by Service"** and **"HTTP Requests by
   Status Code"** panels show the 5xx ratio climbing in real time.

To stop error injection: `curl http://localhost:5001/stop`

---

## 6. Tearing down
```bash
docker compose down -v
```

---

## 7. Suggested structure for the video demo

1. **Architecture (2–3 min):** walk through the diagram above — what scrapes
   what, how alert rules evaluate, how Alertmanager routes by severity.
2. **Dashboard walkthrough (2–3 min):** show live per-service CPU/RAM panels,
   host gauges, and the active-alerts table/stat panel in Grafana.
3. **Alerting & escalation demo (3–5 min):** run the `stress-ng` CPU test
   live, show the warning alert firing in Prometheus → Alertmanager →
   webhook-logger logs, then let it cross into critical and show the
   escalation receiver firing and the warning being inhibited.
4. **Wrap-up (1 min):** mention configurability (thresholds, receivers,
   repeat intervals) and how this maps to production tools (Slack/PagerDuty/
   Opsgenie in place of the webhook stub).

---

## 8. Design notes / trade-offs

- **cAdvisor** was chosen over per-app custom exporters because it gives
  service-level (per-container) CPU/RAM metrics without instrumenting each
  service individually — this is the standard "service-level monitoring"
  approach in containerized DevOps environments.
- **Two-tier severity + `for:` durations** avoids flapping/noisy alerts:
  a short spike doesn't page anyone; sustained breaches do.
- **Inhibition rules** are used instead of hard-coding "only send once" logic,
  keeping the escalation path simple and declarative.
- **Recording rules** (`service:cpu_usage:percent`,
  `service:memory_usage:percent`) precompute the percentage metrics once, so
  both the alert rules and the Grafana panels query the same, cheaper,
  precomputed series instead of duplicating the raw PromQL everywhere.
#   k 8 s  
 