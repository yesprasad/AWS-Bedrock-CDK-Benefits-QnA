import * as cdk from 'aws-cdk-lib';
import { BenefitsStack } from './stacks/benefitsStack';

// instantiate CDK App
const app = new cdk.App();

new BenefitsStack(app, 'BenefitsStack', {
    env: {
        account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
    }
});

app.synth();