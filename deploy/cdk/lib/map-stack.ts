import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as targets from "aws-cdk-lib/aws-route53-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import { Construct } from "constructs";

export interface MapStackProps extends cdk.StackProps {
  /** The Route 53 hosted zone the subdomain goes in, e.g. example.com */
  zoneName: string;
  zoneId: string;
  /** The subdomain's first label, e.g. "pv-map" for pv-map.example.com */
  subdomain: string;
}

const SITE = path.join(__dirname, "..", "..", "bundle", "site");
const WEEK = cdk.Duration.days(7);

/** The subdivision map (deploy/bundle/site, built by `pvc map-bundle`) as static files: a private S3
 * bucket that only CloudFront can read, served over HTTPS at subdomain.zoneName. The stack lives in
 * us-east-1 because CloudFront takes its certificate from there.
 *
 * Three uploads: the page images (docs/<slug>/<n>-<size>.avif, sent as image/avif and cached for
 * a week, since a page's image doesn't change), the GeoJSON, and everything else (the page, its
 * script config, highlights.json and docs/index.json). All but the images are rechecked on each
 * visit, so a redeploy shows at once. Each upload removes the files of its own kind that the
 * bundle no longer has and leaves the others alone. */
export class MapStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MapStackProps) {
    super(scope, id, props);
    const domainName = `${props.subdomain}.${props.zoneName}`;
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, "Zone", {
      hostedZoneId: props.zoneId,
      zoneName: props.zoneName,
    });
    const certificate = new acm.Certificate(this, "Certificate", {
      domainName,
      validation: acm.CertificateValidation.fromDns(zone),
    });

    const bucket = new s3.Bucket(this, "Site", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const distribution = new cloudfront.Distribution(this, "Cdn", {
      comment: domainName,
      domainNames: [domainName],
      certificate,
      defaultRootObject: "index.html",
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
        compress: true,
      },
    });

    const pages = new s3deploy.BucketDeployment(this, "Pages", {
      sources: [s3deploy.Source.asset(path.join(SITE, "docs"), { exclude: ["*.json"] })],
      destinationBucket: bucket,
      destinationKeyPrefix: "docs/",
      exclude: ["*.json"],
      contentType: "image/avif",
      cacheControl: [s3deploy.CacheControl.setPublic(), s3deploy.CacheControl.maxAge(WEEK)],
      memoryLimit: 1024,
    });
    const files = s3deploy.Source.asset(SITE, { exclude: ["**/*.avif"] });
    // GeoJSON as application/json, which CloudFront compresses (S3 would guess octet-stream).
    const data = new s3deploy.BucketDeployment(this, "Data", {
      sources: [files],
      destinationBucket: bucket,
      exclude: ["*"],
      include: ["*.geojson"],
      contentType: "application/json",
      cacheControl: [s3deploy.CacheControl.noCache()],
      memoryLimit: 512,
    });
    const site = new s3deploy.BucketDeployment(this, "Map", {
      sources: [files],
      destinationBucket: bucket,
      exclude: ["*.avif", "*.geojson"],
      cacheControl: [s3deploy.CacheControl.noCache()],
      distribution,
      distributionPaths: ["/*"],
      memoryLimit: 512,
    });
    site.node.addDependency(pages, data);

    for (const [id, Record] of [["A", route53.ARecord], ["Aaaa", route53.AaaaRecord]] as const) {
      new Record(this, id, {
        zone,
        recordName: props.subdomain,
        target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(distribution)),
      });
    }

    new cdk.CfnOutput(this, "Url", { value: `https://${domainName}/` });
    new cdk.CfnOutput(this, "DistributionDomain", { value: distribution.distributionDomainName });
  }
}
