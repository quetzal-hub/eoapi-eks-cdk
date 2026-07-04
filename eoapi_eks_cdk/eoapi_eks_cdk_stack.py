"""CDK stack for the eoAPI EKS cluster.

Provisions the infrastructure layer only: VPC, EKS cluster, and a managed
node group. Everything that runs *inside* the cluster (EBS CSI driver,
ingress-nginx, the Postgres operator, the eoAPI chart, and the observability
stack) is deployed with plain `helm`/`kubectl` after the cluster is up.

Two deliberate omissions, both explained in docs/TROUBLESHOOTING.md:

- No Helm charts are installed from CDK. `add_helm_chart` runs Helm inside a
  Lambda-backed custom resource with a hard 15-minute execution cap that
  eoAPI's slow-settling database bootstrap cannot fit.
- No AWS Load Balancer Controller. The eoAPI chart manages the Ingress and
  restricts `ingress.className` to nginx or traefik (both strip the path
  prefixes the services require), so ingress uses ingress-nginx.
"""

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks_v2 as eks
from constructs import Construct


class EoapiEksCdkStack(Stack):
    """VPC + EKS cluster + managed node group (infrastructure only)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Two AZs (the EKS minimum). A single NAT gateway keeps cost down;
        # for production HA you'd want one per AZ.
        vpc = ec2.Vpc(self, "EoapiEksVpc", max_azs=2, nat_gateways=1)

        cluster = eks.Cluster(
            self,
            "EoapiCluster",
            vpc=vpc,
            version=eks.KubernetesVersion.V1_34,
            # default_capacity=0 so the node group below is the only capacity,
            # with explicit sizing instead of the construct's default.
            default_capacity_type=eks.DefaultCapacityType.NODEGROUP,
            default_capacity=0,
            # Grants the deploying IAM identity cluster-admin via an EKS
            # Access Entry. Without this, a valid AWS identity still has no
            # Kubernetes authorization (see docs/TROUBLESHOOTING.md).
            bootstrap_cluster_creator_admin_permissions=True,
        )

        cluster.add_nodegroup_capacity(
            "EoapiNodeGroup",
            instance_types=[ec2.InstanceType("t3.medium")],
            min_size=2,
            max_size=3,
            desired_size=2,
        )

        CfnOutput(
            self,
            "ClusterName",
            value=cluster.cluster_name,
            description="EKS cluster name (for `aws eks update-kubeconfig`)",
        )
        CfnOutput(
            self,
            "OidcIssuerUrl",
            value=cluster.cluster_open_id_connect_issuer_url,
            description="OIDC issuer URL (needed for IRSA trust policies)",
        )
