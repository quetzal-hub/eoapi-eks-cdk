# Deployment guide

End-to-end walkthrough: from an empty AWS account to a public eoAPI endpoint
with tracing and metrics. Expect the full process to take 45–60 minutes, most
of it waiting on CloudFormation and Helm.

> **Cost note:** this stack runs an EKS control plane (~$0.10/hr), two
> `t3.medium` nodes, a NAT gateway, and one or more load balancers. Tear it
> down when you're done (see [Teardown](#teardown)).

## 0. Prerequisites

- AWS credentials configured (`aws sts get-caller-identity` works)
- Node.js + the AWS CDK CLI (`npm install -g aws-cdk`)
- Python 3.12+, `kubectl`, `helm`
- CDK bootstrapped in the target account/region: `cdk bootstrap`

```bash
python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **On Windows PowerShell:** the commands below are written for bash (the
> convention for AWS/Kubernetes docs, and portable to Mac/Linux/WSL), but
> every `aws`/`kubectl`/`helm` invocation is the identical binary regardless
> of shell; only the wrapping syntax differs. See
> [Running on Windows (PowerShell)](#running-on-windows-powershell) at the
> bottom of this doc for a translation guide and the specific commands that
> need more than a syntax tweak to work.

## 1. Provision the cluster (CDK)

```bash
cdk deploy
```

This creates the VPC, EKS cluster (Kubernetes 1.34), and a managed node group
(2× `t3.medium`). Takes ~20 minutes. Everything that runs inside the cluster
is installed in the steps below with `helm`/`kubectl`.

Point `kubectl` at the new cluster using the `ClusterName` stack output:

```bash
aws eks update-kubeconfig --region <region> --name <ClusterName output>
kubectl get nodes   # expect 2 Ready nodes
```

> **If this returns `Unauthorized` / "the server has asked for the client to
> provide credentials":** you almost certainly deployed through a bootstrapped
> CDK environment, so the access entry from
> `bootstrap_cluster_creator_admin_permissions=True` was granted to the CDK
> bootstrap role, not to you. Grant yourself access explicitly:
>
> ```bash
> ME=$(aws sts get-caller-identity --query Arn --output text)
> aws eks create-access-entry --cluster-name <cluster> --region <region> --principal-arn "$ME"
> aws eks associate-access-policy --cluster-name <cluster> --region <region> --principal-arn "$ME" \
>   --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
>   --access-scope type=cluster
> ```
>
> Full explanation in
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#kubectl-is-unauthorized-after-a-clean-deploy).

## 2. Storage: EBS CSI driver + default StorageClass

EKS ≥1.30 ships **no default StorageClass** and no EBS CSI driver. Without
both, every PersistentVolumeClaim (including Postgres's) hangs in `Pending`.

The CSI driver needs its own IAM role via IRSA, and IRSA needs the cluster's
OIDC issuer registered with IAM as an **OIDC provider** first. This stack does
not create that provider for you (the `eks.Cluster` construct's OIDC *issuer*
always exists, but registering it with IAM is a separate, one-time-per-cluster
step). Check whether it already exists before proceeding:

```bash
aws iam list-open-id-connect-providers
```

If nothing matches your cluster, create it. The simplest path is `eksctl`:

```bash
eksctl utils associate-iam-oidc-provider --cluster <cluster> --region <region> --approve
```

Without `eksctl`, do it with the AWS CLI directly. The `--thumbprint-list`
value must be the SHA-1 fingerprint of the **last** (root) certificate the
issuer's TLS endpoint presents. Compute it; don't guess or reuse a value from
an old tutorial, since AWS has rotated this chain before:

```bash
HOST=oidc.eks.<region>.amazonaws.com
echo | openssl s_client -servername "$HOST" -showcerts -connect "$HOST:443" 2>/dev/null \
  | awk -v n=0 '/-----BEGIN CERTIFICATE-----/{n++} {print > ("/tmp/cert_" n ".pem")}'
LAST=$(ls /tmp/cert_*.pem | sort -V | tail -1)
THUMBPRINT=$(openssl x509 -in "$LAST" -noout -fingerprint -sha1 | sed 's/^.*=//; s/://g' | tr 'A-F' 'a-f')

aws iam create-open-id-connect-provider \
  --url "https://$HOST/id/<OIDC_ID>" \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list "$THUMBPRINT"
```

`<OIDC_ID>` is the trailing path segment of the stack's `OidcIssuerUrl`
output.

Now fill in the placeholders in
[`iam/ebs-csi-trust-policy.template.json`](../iam/ebs-csi-trust-policy.template.json)
(the same `<ACCOUNT_ID>`, `<REGION>`, `<OIDC_ID>` from above; account ID is
`aws sts get-caller-identity --query Account`), save it as
`iam/ebs-csi-trust-policy.json` (already gitignored, since it embeds a real
account ID), then:

```bash
aws iam create-role --role-name AmazonEKS_EBS_CSI_DriverRole \
  --assume-role-policy-document file://iam/ebs-csi-trust-policy.json
aws iam attach-role-policy --role-name AmazonEKS_EBS_CSI_DriverRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy

aws eks create-addon --cluster-name <cluster> --addon-name aws-ebs-csi-driver \
  --service-account-role-arn arn:aws:iam::<account>:role/AmazonEKS_EBS_CSI_DriverRole

kubectl apply -f k8s/gp3-storageclass.yaml
```

> Get the trust policy exactly right: a malformed OIDC ARN produces a
> crash-looping CSI controller with `AccessDenied`. See
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#malformed-oidc-trust-policy).

## 3. Postgres operator

```bash
helm install --set disable_check_for_upgrades=true pgo \
  oci://registry.developers.crunchydata.com/crunchydata/pgo \
  --version 5.8.6 --namespace postgres-operator --create-namespace
```

## 4. eoAPI

```bash
helm repo add eoapi https://devseed.com/eoapi-k8s/
helm install eoapi eoapi/eoapi --namespace eoapi --create-namespace \
  --set ingress.enabled=false --timeout 30m
```

Two deliberate choices here:

- **No `--wait`.** The chart's pgSTAC migration runs as a post-install hook,
  but the API pods' init containers wait *for* that migration, so `--wait`
  deadlocks the install. Details in
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md#helm---wait--hook-deadlock).
- **Ingress disabled** at install; it's enabled in the next step, once the
  nginx controller exists.

Watch it settle:

```bash
kubectl -n eoapi get pods -w
```

## 5. Ingress (nginx)

The eoAPI services are written to serve at `/`, so the ingress must strip the
`/stac`, `/raster`, and `/vector` path prefixes before forwarding. nginx and
traefik do that natively, which is why the eoAPI chart restricts
`ingress.className` to those two. This build lets the chart manage the
Ingress, so the controller is ingress-nginx. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#ingress-is-limited-to-nginx-or-traefik-by-the-chart).

Install the ingress-nginx controller (it provisions an AWS Network Load
Balancer):

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx --create-namespace
```

Then enable the chart's own ingress against that controller:

```bash
helm upgrade eoapi eoapi/eoapi -n eoapi --reuse-values \
  --set ingress.enabled=true --set ingress.className=nginx
```

Get the public load balancer hostname and verify:

```bash
kubectl -n ingress-nginx get svc ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

curl http://<nlb-hostname>/stac/collections
curl http://<nlb-hostname>/raster/healthz
curl http://<nlb-hostname>/vector/healthz
```

## 6. Load sample STAC data

The [`data/`](../data) directory contains a sample collection (Maxar Open
Data, January 2025 Los Angeles wildfires) and ~600 items as newline-delimited
JSON, ready for `pypgstac`.

Build a DSN from the credentials secret, then port-forward and load:

```bash
# Find the Crunchy-generated credentials secret, named <cluster>-pguser-<user>.
# Use the app user (eoapi-pguser-eoapi), not the postgres superuser.
kubectl -n eoapi get secrets | grep pguser
SECRET=eoapi-pguser-eoapi

# Secret data is base64 at rest; kubectl's go-template decodes it inline, so
# there's no dependency on which base64 flavor (or none) is on your PATH.
PGUSER=$(kubectl -n eoapi get secret "$SECRET" -o go-template='{{.data.user | base64decode}}')
PGPASS=$(kubectl -n eoapi get secret "$SECRET" -o go-template='{{.data.password | base64decode}}')
PGDB=$(kubectl -n eoapi get secret "$SECRET" -o go-template='{{.data.dbname | base64decode}}')
DSN="postgresql://${PGUSER}:${PGPASS}@localhost:5432/${PGDB}"

kubectl -n eoapi port-forward svc/<primary-service> 5432:5432 &

pip install "pypgstac[psycopg]"
pypgstac load collections data/collection.json --dsn "$DSN" --method insert_ignore
pypgstac load items data/items.ndjson --dsn "$DSN" --method insert_ignore
```

**PowerShell:** the same `go-template` commands work as-is (`kubectl` is the
same binary in any shell); only the variable syntax changes, and the
backgrounded port-forward moves to its own tab (see
[Running on Windows](#running-on-windows-powershell)):

```powershell
$SECRET = "eoapi-pguser-eoapi"

$PGUSER = kubectl -n eoapi get secret $SECRET -o go-template='{{.data.user | base64decode}}'
$PGPASS = kubectl -n eoapi get secret $SECRET -o go-template='{{.data.password | base64decode}}'
$PGDB   = kubectl -n eoapi get secret $SECRET -o go-template='{{.data.dbname | base64decode}}'
$DSN = "postgresql://${PGUSER}:${PGPASS}@localhost:5432/${PGDB}"

# Run in a separate terminal tab and leave it open (PowerShell has no `&`):
# kubectl -n eoapi port-forward svc/<primary-service> 5432:5432

pip install "pypgstac[psycopg]"
pypgstac load collections data/collection.json --dsn $DSN --method insert_ignore
pypgstac load items data/items.ndjson --dsn $DSN --method insert_ignore
```

Then confirm through the API:

```bash
curl http://<nlb-hostname>/stac/collections/WildFires-LosAngeles-Jan-2025/items?limit=1
```

## 7. Observability

**Metrics.** The eoAPI chart bundles Prometheus and Grafana as optional
subcharts, each behind its own condition flag (`monitoring.prometheus.enabled`
and `observability.grafana.enabled`):

```bash
helm upgrade eoapi eoapi/eoapi -n eoapi --reuse-values \
  --set monitoring.prometheus.enabled=true \
  --set observability.grafana.enabled=true \
  --set observability.grafana.service.type=ClusterIP
```

The last flag overrides the chart's default (`LoadBalancer`), which would
otherwise provision a *second* public AWS load balancer just to view
dashboards. Port-forwarding is simpler and costs nothing extra.

View dashboards:

```bash
kubectl -n eoapi port-forward svc/eoapi-grafana 3000:80
# open http://localhost:3000
```

Get the admin credentials from the chart-generated secret. Its keys are
hyphenated (`admin-user`, `admin-password`), which `go-template`'s dot
notation can't parse (`.data.admin-user` reads as subtraction), so use `index`
instead:

```bash
kubectl -n eoapi get secret eoapi-grafana -o go-template='{{index .data "admin-user" | base64decode}}'
kubectl -n eoapi get secret eoapi-grafana -o go-template='{{index .data "admin-password" | base64decode}}'
```

**PowerShell:** this one needs the same backslash-escape as the OTel patch
loop below, because PowerShell strips the double quotes around `"admin-user"`
before `kubectl` sees them (it fails with `bad character U+002D '-'`, the same
error as leaving the key unquoted, which is exactly what PowerShell reduces it
to):

```powershell
kubectl -n eoapi get secret eoapi-grafana -o go-template='{{index .data \"admin-user\" | base64decode}}'
kubectl -n eoapi get secret eoapi-grafana -o go-template='{{index .data \"admin-password\" | base64decode}}'
```

Grafana reaches Prometheus over the cluster's internal DNS, not a
port-forward. The general pattern is always
`<service>.<namespace>.svc.cluster.local`, which here is
`eoapi-prometheus-server.eoapi.svc.cluster.local` (service name found with
`kubectl -n eoapi get svc | grep -i prometheus`; the chart names subchart
resources `<release>-<subchart>-<component>`, hence `eoapi-prometheus-server`).
This datasource is already pre-wired by the chart's own defaults
(`url: "http://{{ .Release.Name }}-prometheus-server"`), so no manual setup is
needed in Grafana at all.

**Tracing.** OpenTelemetry auto-instrumentation feeds Jaeger with zero
application code changes:

```bash
# cert-manager (required by the OTel operator's webhooks)
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager -n cert-manager \
  --create-namespace --set crds.enabled=true

# OpenTelemetry operator
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm install opentelemetry-operator open-telemetry/opentelemetry-operator \
  -n opentelemetry-operator-system --create-namespace

# Wait for the operator to be ready before using its CRD (see the note below).
kubectl -n opentelemetry-operator-system rollout status deployment/opentelemetry-operator

# Jaeger all-in-one + the Instrumentation resource
kubectl apply -f k8s/jaeger.yaml
kubectl apply -f k8s/otel-instrumentation.yaml
```

> **If `kubectl apply -f k8s/otel-instrumentation.yaml` fails with `no
> endpoints available for service "opentelemetry-operator-webhook"`:** this is
> a startup race, not a config problem. `helm install` (no `--wait` above)
> returns as soon as the Deployment object is *created*, not once the operator
> pod is `Ready` and its admission webhook has registered an endpoint.
> Applying the `Instrumentation` resource (which that webhook must intercept)
> in that gap fails with exactly this error. The `rollout status` line above
> closes the gap; if you still hit it, wait and retry:
>
> ```bash
> kubectl -n opentelemetry-operator-system rollout status deployment/opentelemetry-operator
> kubectl apply -f k8s/otel-instrumentation.yaml
> ```
>
> The resource was never malformed, just early.

Then opt each eoAPI service into injection:

```bash
for d in eoapi-stac eoapi-raster eoapi-vector; do
  kubectl -n eoapi patch deployment $d -p \
    '{"spec":{"template":{"metadata":{"annotations":{"instrumentation.opentelemetry.io/inject-python":"true"}}}}}'
done
```

> The Instrumentation resource points at Jaeger's **4318** (http/protobuf)
> port, not 4317 (gRPC). The Python auto-instrumentation exports http/protobuf
> by default, and a gRPC endpoint fails *silently*. See
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#silent-opentelemetry-port-mismatch).

View traces:

```bash
kubectl -n eoapi port-forward svc/jaeger 16686:16686
# open http://localhost:16686
```

## Teardown

Order matters, because Kubernetes-provisioned load balancers can outlive the
stack that indirectly created them:

```bash
helm uninstall eoapi -n eoapi                     # removes the chart's ingress
helm uninstall ingress-nginx -n ingress-nginx     # deletes the NLB
helm uninstall pgo -n postgres-operator
helm uninstall opentelemetry-operator -n opentelemetry-operator-system
helm uninstall cert-manager -n cert-manager

aws eks delete-addon --cluster-name <cluster> --addon-name aws-ebs-csi-driver
aws iam detach-role-policy --role-name AmazonEKS_EBS_CSI_DriverRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy
aws iam delete-role --role-name AmazonEKS_EBS_CSI_DriverRole

cdk destroy
```

Afterward, verify in the console that the cluster, node group, **all load
balancers**, and any leftover EBS volumes are gone.

## Running on Windows (PowerShell)

Every command above invokes `aws`, `kubectl`, `helm`, or `pip`, the same
binary regardless of shell. Most blocks need no change beyond the wrapping
syntax. Verified directly against Windows PowerShell 5.1:

| bash | PowerShell |
|---|---|
| `cmd1 && cmd2` | not supported; put on separate lines, or `cmd1; if ($?) { cmd2 }` |
| line continuation `\` | `` ` `` (backtick), with **no trailing whitespace after it** |
| `VAR=value` | `$VAR = "value"` |
| `export VAR=value` | `$env:VAR = "value"` |
| `$(cmd)` / `VAR=$(cmd)` | same, or just `$VAR = cmd` (no `$()` needed) |
| `for x in a b c; do ..; done` | `foreach ($x in "a","b","c") { .. }` |
| `cmd \| grep foo` | `cmd \| Select-String foo` |
| `2>/dev/null` | `2>$null` |
| `cmd &` (background) | **hard parse error** (`AmpersandNotAllowed`) if pasted as-is; see below |

A few commands need more than a syntax swap. These are tested, not guessed:

**The port-forward in step 6.** `kubectl -n eoapi port-forward svc/<x>
5432:5432 &` will not even parse; a trailing `&` throws `AmpersandNotAllowed`
in Windows PowerShell (it's reserved, not a background operator here). Drop the
`&`, run that one command in a second terminal tab in the foreground, and run
the `pypgstac load` commands in your original tab against `localhost:5432`.

**The OTel injection loop (step 7).** Windows PowerShell strips embedded double
quotes from a single-quoted string before handing it to *any* native
executable, confirmed by inspecting the raw argv a process receives. The JSON
in `kubectl patch -p '{"spec":...}'` arrives with every `"` gone
(`{spec:{template:true}}`), and `kubectl` rejects it. Escape the inner quotes
with a backslash, which survives intact:

```powershell
foreach ($d in "eoapi-stac","eoapi-raster","eoapi-vector") {
  kubectl -n eoapi patch deployment $d -p '{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"instrumentation.opentelemetry.io/inject-python\":\"true\"}}}}}'
}
```

or sidestep command-line quoting entirely with `--patch-file`:

```powershell
'{"spec":{"template":{"metadata":{"annotations":{"instrumentation.opentelemetry.io/inject-python":"true"}}}}}' |
  Out-File -Encoding utf8 patch.json
foreach ($d in "eoapi-stac","eoapi-raster","eoapi-vector") {
  kubectl -n eoapi patch deployment $d --patch-file patch.json
}
Remove-Item patch.json
```

**The OIDC thumbprint script (step 2).** `openssl` and `awk` aren't on a
default PowerShell `PATH`, even with Git for Windows installed (they live under
Git's own `mingw64\bin`). This uses .NET directly instead, verified against a
live OIDC endpoint:

```powershell
$hostname = "oidc.eks.<region>.amazonaws.com"
$tcp = New-Object System.Net.Sockets.TcpClient($hostname, 443)
$ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, ({$true}))
$ssl.AuthenticateAsClient($hostname)
$chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
$chain.Build($ssl.RemoteCertificate) | Out-Null
$root = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate
$thumbprint = $root.GetCertHashString("SHA1").ToLower()
$ssl.Close(); $tcp.Close()

aws iam create-open-id-connect-provider `
  --url "https://$hostname/id/<OIDC_ID>" `
  --client-id-list sts.amazonaws.com `
  --thumbprint-list $thumbprint
```

If `Activate.ps1` refuses to run ("running scripts is disabled on this
system"), that's the default execution policy, not a broken venv:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # this shell only, no machine-wide change
```

or skip activation entirely and call `.venv\Scripts\python.exe` /
`.venv\Scripts\pip.exe` directly.
