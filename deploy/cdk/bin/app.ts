#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { MapStack } from "../lib/map-stack";

const app = new cdk.App();
const need = (key: string): string => {
  const v = app.node.tryGetContext(key);
  if (!v) throw new Error(`Set -c ${key}=... (or "${key}" in cdk.json's context)`);
  return v;
};

new MapStack(app, "PvSubdivisionMap", {
  zoneName: need("zoneName"),
  zoneId: need("zoneId"),
  subdomain: need("subdomain"),
  vpcId: app.node.tryGetContext("vpcId"),
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  description: "Prairie Village subdivision restrictions map (council-packets)",
});
