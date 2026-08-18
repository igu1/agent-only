# Kubernetes deployment (SaaS Phase 4)

The Phase 3 stack (gateway + Redis queue + chat workers + Qdrant), expressed
as Kubernetes manifests. Same Docker image as compose — nothing rebuilds.
Adopt only at the Phase 4 triggers (multi-node, autoscaling, zero-downtime
requirement); until then `docker-compose.queue.yml` runs the identical
architecture on one server.

## Install (single server: k3s)

```bash
curl -sfL https://get.k3s.io | sh -                 # includes traefik ingress + metrics-server
docker build -t cronoagent:v1.0 /opt/cronoagent-src
docker save cronoagent:v1.0 | sudo k3s ctr images import -   # local image into k3s
```

cert-manager for automatic HTTPS (replaces certbot):

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
# then create a ClusterIssuer named "letsencrypt" (ACME HTTP-01)
```

## Deploy

```bash
cd deploy/k8s
cp secrets.example.yaml secrets.yaml     # fill real tenants.json + never commit
kubectl apply -f secrets.yaml
kubectl apply -k .
kubectl -n cronoagent get pods           # gateway x2, worker x2, redis, qdrant
```

## Day-2

```bash
kubectl -n cronoagent logs deploy/worker -f            # worker logs
kubectl -n cronoagent rollout restart deploy/gateway   # bounce
kubectl -n cronoagent set image deploy/gateway gateway=cronoagent:v1.1   # rolling update
kubectl -n cronoagent set image deploy/worker  worker=cronoagent:v1.1
kubectl -n cronoagent rollout undo deploy/gateway      # instant rollback
kubectl -n cronoagent get hpa                          # autoscaler state
```

Onboard an org: edit the `agent-tenants` Secret (or re-apply secrets.yaml),
then `curl -X POST .../infra/reload-tenants -H "X-Agent-Token: <org token>"`.
Secret volume updates propagate to pods within ~1 minute.

## Sizing / caveats (mirror SAAS-ARCHITECTURE.md)

- Worker HPA is CPU-based (metrics-server ships with k3s). Queue-depth-based
  scaling needs a custom metrics adapter — later.
- Before maxReplicas > 3 workers: move agno chat memory from the shared
  sqlite PVC to Postgres (ReadWriteOnce PVC also pins workers to one node —
  another reason Postgres precedes real multi-node scaling).
- Voice turns process in-gateway (bypass the queue) — voice-heavy load
  stays on gateway replicas.
- Redis is intentionally ephemeral: in-flight turns only; conversations are
  durable in each org's SQL.
