# Troubleshooting log

Real problems hit during this deployment, with root causes and fixes. Kept as
a reference because each one is a class of failure, not a one-off.

## CDK's Helm mechanism has structural ceilings

**Symptom:** deploying the eoAPI chart via `cluster.add_helm_chart(...)` fails,
either with a CloudFormation custom-resource response error or a timeout,
regardless of the chart's own `--timeout`.

**Root cause:** `add_helm_chart` runs `helm install` inside a Lambda-backed
CloudFormation custom resource. That imposes two hard limits that no chart
setting can lift:

1. Lambda's **15-minute maximum execution time**: a chart whose database
   bootstrap settles slowly cannot finish inside it.
2. The custom-resource **response size limit**, which large chart outputs can
   overflow.

**Fix / consequence:** this is *why* the project splits responsibilities. CDK
owns infrastructure (VPC, cluster, node group); everything inside the cluster
is installed with plain `helm` and `kubectl`. The split is forced by the
platform, and it also mirrors how production teams typically structure EKS
deployments (infra pipeline vs. app delivery).

## kubectl is unauthorized after a clean deploy

**Symptom:** immediately after a successful `cdk deploy` and a successful
`aws eks update-kubeconfig`, every `kubectl` command fails:

```
E0720 ... "Unhandled Error" err="couldn't get current server API group list: the server has asked for the client to provide credentials"
```

`aws sts get-caller-identity` proves your credentials are valid, so this looks
paradoxical: the identity is fine, but the API server rejects it anyway.

**Root cause:** being a valid AWS identity only *authenticates* you to EKS.
*Authorization* inside the cluster requires an explicit **EKS Access Entry**
(the current mechanism, which replaces the legacy `aws-auth` ConfigMap). The
stack sets `bootstrap_cluster_creator_admin_permissions=True` to create one
automatically, but that flag grants cluster-admin to whichever IAM principal
actually calls the EKS `CreateCluster` API, which is not necessarily you. In a
bootstrapped environment (the normal case, once `cdk bootstrap` has run),
`cdk deploy` hands the CloudFormation template to the bootstrap's execution
role, and CloudFormation performs `CreateCluster` as *that role*. Confirmed on
this project's own deploy: `aws eks list-access-entries` showed only the node
group role, the EKS service-linked role, and:

```
arn:aws:iam::<account>:role/cdk-hnb659fds-cfn-exec-role-<account>-<region>
```

The human IAM user was never in the list, so `kubectl`, authenticating as that
user via the `aws eks get-token` exec plugin, gets a bare 401. That message is
kubectl's generic wrapper for *any* rejected credential, whether the principal
is unrecognized or merely unauthorized.

**Fix:** grant your own principal an Access Entry explicitly. Any other
principal that needs in-cluster access (a teammate, a CI role) gets one the
same way:

```bash
ME=$(aws sts get-caller-identity --query Arn --output text)
aws eks create-access-entry --cluster-name <cluster> --principal-arn "$ME"
aws eks associate-access-policy --cluster-name <cluster> --principal-arn "$ME" \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster
```

**Lesson:** "the deploying identity" is ambiguous with any tool that runs
through an intermediary execution role (CDK's bootstrap roles, a CI/CD
pipeline role, a CloudFormation service role). The flag does exactly what it
says; it just isn't *you* it refers to, unless you deploy with long-lived
credentials and no execution role in the chain. When two systems disagree
about who you are, list what each one actually recorded rather than assuming.

## The IAM OIDC provider was never registered

**Symptom:** following the EBS CSI driver setup for the first time,
`aws iam list-open-id-connect-providers` comes back empty even though the
cluster has been up for a while and its `OidcIssuerUrl` output is populated.
Any IRSA trust policy written against that issuer will fail with
`AccessDenied`, because the `Federated` principal it names doesn't exist yet.

**Root cause:** an EKS cluster's OIDC **issuer** (the URL that signs
service-account tokens) exists the moment the cluster is created, but that is
not the same thing as an IAM **OIDC provider** (the object that tells IAM to
trust tokens from that issuer). The two are easy to conflate. This stack's
`eks.Cluster` construct never registers the provider, and nothing in a plain
`cdk deploy` does it for you. (`eksctl` auto-associates the provider, and the
upstream eoapi-k8s AWS guide even scripts a check for it, so this only surfaces
on the raw-CDK path.)

**How it was found:** `aws iam list-open-id-connect-providers` returned an
empty list immediately after `aws eks describe-cluster` showed a live OIDC
issuer for the same cluster. The two should agree once IRSA is set up, and
they didn't.

**Fix:** register the provider once per cluster, either with `eksctl`:

```bash
eksctl utils associate-iam-oidc-provider --cluster <cluster> --approve
```

or with the AWS CLI, using the SHA-1 thumbprint of the OIDC endpoint's root
TLS certificate (see [DEPLOYMENT.md](DEPLOYMENT.md#2-storage-ebs-csi-driver--default-storageclass)
for the exact commands). Don't reuse a thumbprint from an old tutorial;
compute it live, since AWS has rotated this chain across regions before.

**Lesson:** treat "the cluster has an OIDC issuer" and "IAM trusts that
issuer" as two separate, independently-verifiable facts, and check both with
the CLI before writing a trust policy against either.

## Malformed OIDC trust policy

**Symptom:** the EBS CSI driver's controller pods crash-loop; logs show
`AccessDenied` on `sts:AssumeRoleWithWebIdentity`. All PVCs stay `Pending`.

**Root cause:** the hand-built IAM trust policy's `Federated` ARN was missing
its `oidc.eks.<region>.amazonaws.com/id/` prefix, so it pointed at a provider
that didn't exist, and STS refused the web-identity assumption. (`eksctl
create iamserviceaccount --role-only` builds this trust policy for you;
hand-building it on the raw-CDK path is what exposed the typo.)

**How it was found:** listing the account's registered OIDC providers
(`aws iam list-open-id-connect-providers`) and comparing the registered ARN
against the trust policy **line by line**.

**Fix:** correct the ARN (see
[`iam/ebs-csi-trust-policy.template.json`](../iam/ebs-csi-trust-policy.template.json)
for the known-good shape) and update the role:

```bash
aws iam update-assume-role-policy --role-name AmazonEKS_EBS_CSI_DriverRole \
  --policy-document file://iam/ebs-csi-trust-policy.json
```

## Storage isn't automatic on EKS ≥1.30

**Symptom:** every PersistentVolumeClaim sits in `Pending`;
`kubectl get storageclass` returns nothing (or nothing marked default).

**Root cause:** two separate gaps on modern EKS. No default StorageClass ships
with the cluster, and the EBS CSI driver is not installed by default; it's an
add-on with its own IAM requirements (IRSA). Both are documented EKS setup
steps (eksctl and the upstream AWS guide install the add-on as standard); the
raw-CDK build just does them explicitly.

**Fix:** install the `aws-ebs-csi-driver` add-on with a properly-trusted IAM
role, then apply [`k8s/gp3-storageclass.yaml`](../k8s/gp3-storageclass.yaml),
which creates a `gp3` StorageClass annotated as cluster default.

## Ingress is limited to nginx or traefik by the chart

**Context:** the eoAPI services serve at their own root (`/`), so an ingress
fronting them at `/stac`, `/raster`, and `/vector` must strip that prefix
before forwarding. Otherwise a request for `/stac/collections` reaches the
STAC service as `/stac/collections`, which it has no route for, and 404s.

**Constraint:** in this build the eoAPI Helm chart manages the Ingress, and
the chart's values schema restricts `ingress.className` to `nginx` or
`traefik`. Both strip path prefixes natively via an annotation, which is why
the chart limits the class to them; setting it to anything else (for example
`alb`) is rejected. So with the chart owning the Ingress, ingress-nginx is the
controller.

**Fix:** install the ingress-nginx controller and enable the chart's own
ingress against it with `--set ingress.className=nginx`. Installing the
controller is itself a standard step in the upstream AWS EKS guide; the
non-obvious part is only that the chart's schema fixes the class to
nginx/traefik.

**Tradeoff:** the nginx controller provisions a **Network Load Balancer**
rather than an ALB, so the ingress is portable and cloud-agnostic rather than
AWS-native. Fronting the services with an ALB instead would mean disabling the
chart's ingress and managing an ALB Ingress manifest yourself, outside the
chart's schema. The CDK stack no longer installs the AWS Load Balancer
Controller.

## Helm `--wait` / hook deadlock

**Symptom:** `helm install eoapi ... --wait` hangs until timeout. The API
deployments never become ready; the migration job never appears.

**Root cause:** a circular dependency created by `--wait` itself:

- the chart's pgSTAC **migration runs as a post-install hook**;
- the API deployments' **init containers wait for the migration** to finish;
- `--wait` makes Helm **withhold hooks until all resources are ready**, which
  they never will be, because they're waiting on the hook.

**Fix:** drop `--wait` and rely on `kubectl get pods -w` (or a readiness check)
instead. A chart-side fix would be moving the migration to a plain Job or an
init flow that isn't hook-gated.

## OTel operator webhook race: applied too soon after `helm install`

**Symptom:** `kubectl apply -f k8s/otel-instrumentation.yaml` fails
immediately after installing the OpenTelemetry Operator:

```
Error from server (InternalError): error when creating "k8s/otel-instrumentation.yaml":
Internal error occurred: failed calling webhook "minstrumentation.kb.io": failed to call
webhook: Post "https://opentelemetry-operator-webhook.opentelemetry-operator-system.svc:443/...":
no endpoints available for service "opentelemetry-operator-webhook"
```

**Root cause:** the mirror image of the Helm `--wait` deadlock above, this
time caused by the *absence* of waiting. `helm install opentelemetry-operator
...` (no `--wait`) returns as soon as the Deployment object is created, not
once the operator pod is actually `Ready`. The `Instrumentation` CRD is gated
by a mutating admission webhook the operator registers on startup; applying it
in the gap between "Deployment created" and "pod Ready, webhook endpoint
registered" fails, because the API server has a Service to route the webhook
call to but zero Pod endpoints behind it. `no endpoints available` is
Kubernetes describing exactly that gap.

**How it was found:** checked `kubectl get pods` and `kubectl get endpoints`
in the operator's namespace directly. The operator pod was healthy with zero
restarts, and the webhook Service already had an endpoint by the time of
inspection, so this was a timing race that had already resolved itself, not a
persistent misconfiguration.

**Fix:** wait for the operator's rollout before applying anything gated by its
webhook. No change to the YAML is needed; a plain retry succeeds:

```bash
kubectl -n opentelemetry-operator-system rollout status deployment/opentelemetry-operator
kubectl apply -f k8s/otel-instrumentation.yaml
```

**Lesson:** `helm install` without `--wait` returns as soon as objects exist,
not once they're functional, and this cuts both ways. The app chart in this
project needs `--wait` *dropped* to avoid a hook deadlock, while an operator
with an admission webhook needs an *explicit* wait immediately after install,
or anything depending on its CRD in the same script can lose the race. There's
no universal answer: check what a chart's `--wait` actually gates before
deciding whether you want it.

## Silent OpenTelemetry port mismatch

**Symptom:** auto-instrumentation is injected (visible in the pod spec), the
services work normally, but Jaeger shows **zero traces**. No errors anywhere
obvious.

**Root cause:** the OTLP endpoint pointed at Jaeger's **4317** (gRPC) port, but
the Python auto-instrumentation defaults to the **http/protobuf** protocol,
which belongs on **4318**. Every export failed, and the exporter's failure
mode is silent from the operator's point of view.

**How it was found:** `kubectl exec` into a running API container and reading
the injected `OTEL_*` environment variables. The protocol default and the
configured endpoint contradicted each other.

**Fix:** point the exporter at `http://jaeger.eoapi.svc.cluster.local:4318`
(as [`k8s/otel-instrumentation.yaml`](../k8s/otel-instrumentation.yaml) now
does), or explicitly set `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` if 4317 is
intended.
