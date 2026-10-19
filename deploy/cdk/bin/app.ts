#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { MapStack } from "../lib/map-stack";

const app = new cdk.App();
const need = (key: string): string => {
  const v = app.node.tryGetContext(key);
  if (!v) throw new Error(`Set -c ${key}=... (or "${key}" in cdk.json's context)`);
  return v;
};

// us-east-1: CloudFront only uses certificates from there.
new MapStack(app, "PvSubdivisionMap", {
  zoneName: need("zoneName"),
  zoneId: need("zoneId"),
  subdomain: need("subdomain"),
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: "us-east-1" },
  description: "Prairie Village subdivision restrictions map (council-packets)",
});
