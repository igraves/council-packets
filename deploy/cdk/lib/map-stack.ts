import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecsPatterns from "aws-cdk-lib/aws-ecs-patterns";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";

export interface MapStackProps extends cdk.StackProps {
  /** The Route 53 hosted zone the subdomain goes in, e.g. example.com */
  zoneName: string;
  zoneId: string;
  /** The subdomain's first label, e.g. "pv-map" for pv-map.example.com */
  subdomain: string;
  /** An existing VPC with public subnets; without one the stack makes a two-AZ public-only VPC */
  vpcId?: string;
}

/** The subdivision map (deploy/bundle, built by `pvc map-bundle`) on ECS Fargate (ARM64) behind an
 * HTTPS load balancer at subdomain.zoneName. Tasks run in public subnets with a public IP, so no
 * NAT gateway is needed to pull the image; only the load balancer accepts traffic from outside. */
export class MapStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MapStackProps) {
    super(scope, id, props);
    const domainName = `${props.subdomain}.${props.zoneName}`;
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, "Zone", {
      hostedZoneId: props.zoneId,
      zoneName: props.zoneName,
    });
    const vpc = props.vpcId
      ? ec2.Vpc.fromLookup(this, "Vpc", { vpcId: props.vpcId })
      : new ec2.Vpc(this, "Vpc", {
          maxAzs: 2,
          natGateways: 0,
          subnetConfiguration: [{ name: "public", subnetType: ec2.SubnetType.PUBLIC }],
        });
    const cluster = new ecs.Cluster(this, "Cluster", { vpc });
    const certificate = new acm.Certificate(this, "Certificate", {
      domainName,
      validation: acm.CertificateValidation.fromDns(zone),
    });

    const service = new ecsPatterns.ApplicationLoadBalancedFargateService(this, "Map", {
      cluster,
      cpu: 512,
      memoryLimitMiB: 1024,
      desiredCount: 1,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskImageOptions: {
        image: ecs.ContainerImage.fromAsset(path.join(__dirname, "..", "..", "bundle"), {
          platform: ecrAssets.Platform.LINUX_ARM64,
        }),
        containerPort: 8080,
        logDriver: ecs.LogDrivers.awsLogs({
          streamPrefix: "map",
          logRetention: logs.RetentionDays.ONE_MONTH,
        }),
      },
      taskSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      assignPublicIp: true,
      publicLoadBalancer: true,
      domainName,
      domainZone: zone,
      certificate,
      protocol: elbv2.ApplicationProtocol.HTTPS,
      redirectHTTP: true,
      sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS,
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
    });
    service.targetGroup.configureHealthCheck({ path: "/healthz", healthyHttpCodes: "200" });

    new cdk.CfnOutput(this, "Url", { value: `https://${domainName}/` });
  }
}
