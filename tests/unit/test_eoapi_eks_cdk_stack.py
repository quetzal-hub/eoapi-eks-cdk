"""Assertions against the synthesized CloudFormation template."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions
import pytest

from eoapi_eks_cdk.eoapi_eks_cdk_stack import EoapiEksCdkStack


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    app = cdk.App()
    stack = EoapiEksCdkStack(app, "eoapi-eks-cdk-test")
    return assertions.Template.from_stack(stack)


def test_vpc_spans_two_azs_with_single_nat(template):
    template.resource_count_is("AWS::EC2::VPC", 1)
    template.resource_count_is("AWS::EC2::NatGateway", 1)


def test_eks_cluster_version(template):
    template.has_resource_properties(
        "AWS::EKS::Cluster",
        {"Version": "1.34"},
    )


def test_node_group_sizing(template):
    template.has_resource_properties(
        "AWS::EKS::Nodegroup",
        {
            "InstanceTypes": ["t3.medium"],
            "ScalingConfig": {"MinSize": 2, "MaxSize": 3, "DesiredSize": 2},
        },
    )


def test_cluster_admin_access_entry_for_creator(template):
    # bootstrap_cluster_creator_admin_permissions=True must materialize as
    # an EKS Access Entry — IAM identity alone grants no k8s authorization.
    template.has_resource_properties(
        "AWS::EKS::Cluster",
        {
            "AccessConfig": {
                "BootstrapClusterCreatorAdminPermissions": True,
            }
        },
    )


def test_outputs_present(template):
    template.has_output("ClusterName", {})
    template.has_output("OidcIssuerUrl", {})
