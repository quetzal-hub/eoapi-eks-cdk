#!/usr/bin/env python3
"""CDK app entry point for the eoAPI EKS deployment."""

import os

import aws_cdk as cdk

from eoapi_eks_cdk.eoapi_eks_cdk_stack import EoapiEksCdkStack

app = cdk.App()
EoapiEksCdkStack(
    app,
    "EoapiEksCdkStack",
    description="eoAPI on EKS: VPC, cluster, node group",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
