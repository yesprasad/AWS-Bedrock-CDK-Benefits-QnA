import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

/**
 * define the class that extends from Stack to define IaC. 
 * we need S3Vectors with VectorBucket, VectorIndex for storing embeddings along with its related chunks and metadata
 * S3Vectors are similar to what we have with ChromaDB - the behavior is the same but the way we work with S3V is diff
 * VectorBucket == Client of ChromaDB - is a container used by AWS for technical grouping purporses
 * VectorIndex == embeddings saved to ChromaDB. we interact with VectorIndex whenever LLM needs to fetch info for a user prompt.
 * IAM account for defining roles to access
 * Bedrock cfn for connecting with foundational models - 
 * guardrails cfn for stopping what the LLM cannot answer and PII masking using 
 * I will use amazong TitanV2 with 1024 dimensions for extracting vector embeddings. 
 * this is similar to sentence transformers i previously used with Ollama which has 384 dimensions
 * the LLM would be Amzon Micro similar to Qwen2.5:7b but the one from AWS is fully managed. 
 * I would need a lambda to converse with LLM put_vectors() and get_vectors() similar to add() and query() in ChromaDB respectively
 * I need apigateway to get a public API to send the prompt payload to Bedrock to ask questions
 */
export class BenefitsStack extends cdk.Stack {
    constructor(scope: Construct, id: 'BenefitsStack', props?: cdk.StackProps) {
        super(scope, id, props);

        const { vectorBucket, vectorIndex } = this.createS3VectorStorage();
        const { guardrails, guardrailsVersion } = this.createGuardrails();
        const queryFn = this.createLambda(guardrails,guardrailsVersion,vectorIndex);
        this.createApi(queryFn);
        this.createOutputs(guardrails);
    }

    /**
     * Create Bedrock Guardrails for:
     * - topic restriction: only health insurance benefits questions are allowed
     * - PII handling: redact sensitive personal data
     *
     * Guardrails are useful when the assistant should stay in a narrow domain and
     * avoid answering unsafe or out-of-scope questions.
    */
    private createGuardrails() {

        const guardrails = new cdk.aws_bedrock.CfnGuardrail(this, 'BenefitsGuardrails', {
            name: `benefits-guardrails-${this.account}`,
            blockedInputMessaging: ('I can only answer questions about your health insurace benefits plan. ' +
                'Please ask about health insurance, dental, vision.'),
            blockedOutputsMessaging: (
                'I was unable to generate a safe response. Please rephrase your question.'
            ),
            topicPolicyConfig: {
                topicsConfig: [{
                    name: 'NonBenefitsTopics',
                    definition: (
                        'Any question not related to health insurance benefits. ' +
                        'This includes financial advice, medical diagnosis, legal advice, ' +
                        'general knowledge questions, coding help, or anything unrelated'
                    ),
                    type: 'DENY',
                }]
            },
            sensitiveInformationPolicyConfig: {
                piiEntitiesConfig: [
                    { action: 'ANONYMIZE', type: 'PHONE' },
                    { action: 'ANONYMIZE', type: 'EMAIL' },
                    { action: 'ANONYMIZE', type: 'US_SOCIAL_SECURITY_NUMBER' },
                    { action: 'ANONYMIZE', type: 'NAME' },
                    { action: 'ANONYMIZE', type: 'ADDRESS' },
                    { action: 'ANONYMIZE', type: 'CREDIT_DEBIT_CARD_NUMBER' },
                ],
            },
        });
        /**
         * Guardrail versions create a snapshot of the policy once the configuration is ready.
         * This is useful when you want a stable, deployable version instead of editing in place.
         */
        const guardrailsVersion = new cdk.aws_bedrock.CfnGuardrailVersion(
            this,
            'BenefitsGuardrailsVersion',
            {
                guardrailIdentifier: guardrails.attrGuardrailId,
            }
        );

        return { guardrails, guardrailsVersion }
    }


    /*
        S3 vectors inside in the same group as Amazon S3 under the Buckets in the Vector Buckets category
        every vector Bucket has indexes inside it and then 
        we define Indexes as depends to a Bucket we want it to belong to calling the addDependency
        biggest advantage is we get to work on embeddings without provisioning any additional infrastructure
    */
    private createS3VectorStorage(): { vectorBucket: cdk.CfnResource; vectorIndex: cdk.CfnResource; } {
        //S3Vectors currently do not have L2 construct so we create the resource using L1 constructs
        /*
            A Bucket is a container/wrapper for AWS to route and search where the indexes are stored
        */
        const vectorBucket = new cdk.CfnResource(this, 'BenefitsVectorBucket', {
            type: 'AWS::S3Vectors::VectorBucket',
            properties: {
                VectorBucketName: `benefits-vectors-${this.account}`
            }
        });
        /**
                 * vector Indexes is where the magic happens - the strings that are converted to numbers by 
                 * TitanV2 into 1024 dimension - which is nothing but 1024 1row & 1024 column array
                 * The index is a collection of such vectors called embeddings + metadata
                 * distance metric- cosine means - the similarity search is based on cosine.
                 * there are other ways of similarity search too - like Ecueldian and others
                 * Vector Bucket  (benefits-vectors-123456789)
                    │
                    └── Vector Index  (benefits-index)
                            │
                            ├── chunk_0001  →  [0.021, -0.184, 0.009, ...]  1024 floats  +  metadata
                            ├── chunk_0002  →  [0.114,  0.033, -0.071, ...]  1024 floats  +  metadata
                            ├── chunk_0042  →  [0.023, -0.183, 0.011, ...]  1024 floats  +  metadata
                            └── ...
        */
        const vectorIndex = new cdk.CfnResource(this, 'BenefitsVectorIndex', {
            type: 'AWS::S3Vectors::Index',
            properties: {
                VectorBucketName: `benefits-vectors-${this.account}`,
                IndexName: 'benefits-index',
                DataType: 'float32',
                Dimension: 1024,
                DistanceMetric: 'cosine',
            }
        });

        vectorIndex.addDependency(vectorBucket);

        return { vectorBucket, vectorIndex };
    }

    private createLambda(guardrail: cdk.aws_bedrock.CfnGuardrail,
        guardrailVersion: cdk.aws_bedrock.CfnGuardrailVersion,
        vectorIndex: cdk.CfnResource): cdk.aws_lambda.Function {
        const queryFn = new cdk.aws_lambda.Function(this, 'QueryFn', {
            runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
            handler: 'query.handle',
            code: cdk.aws_lambda.Code.fromAsset('lambda/query.zip'),
            timeout: cdk.Duration.seconds(30),
            memorySize: 512,
            environment: {
                VECTOR_BUCKET_NAME: `benefits-vectors-${this.account}`,
                VECTOR_INDEX_NAME: 'benefits-index',
                GUARDRAIL_ID: guardrail.attrGuardrailId,
                GUARDRAIL_VERSION: guardrailVersion.attrVersion,
                AWS_REGION_NAME: this.region,
            },
        });

        // Explicit dependency — Lambda won't be created until the index is ready
        queryFn.node.addDependency(vectorIndex);

        // S3 Vectors uses the `s3vectors` IAM namespace — not `s3`
        queryFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
            actions: ['s3vectors:QueryVectors', 's3vectors:GetVectors'],
            resources: [
                `arn:aws:s3vectors:${this.region}:${this.account}:bucket/benefits-vectors-${this.account}/index/benefits-index`,
            ],
        }));

        queryFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
            actions: ['bedrock:InvokeModel'],
            resources: [
                `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-text-v2:0`,
                `arn:aws:bedrock:${this.region}::foundation-model/amazon.nova-micro-v1:0`,
            ],
        }));

        queryFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
            actions: ['bedrock:ApplyGuardrail'],
            resources: [guardrail.attrGuardrailArn],
        }));

        return queryFn;
    }

    private createApi(queryFn: cdk.aws_lambda.Function) {
        const api = new cdk.aws_apigateway.RestApi(this, 'BenefitsApi', {
            restApiName: 'benefits-explainer',
            defaultCorsPreflightOptions: {
                allowOrigins: cdk.aws_apigateway.Cors.ALL_ORIGINS,
                allowMethods: ['POST', 'OPTIONS'],
            },
        });
        api.root.addResource('ask').addMethod('POST', new cdk.aws_apigateway.LambdaIntegration(queryFn));
    }

    private createOutputs(guardrail: cdk.aws_bedrock.CfnGuardrail) {
        new cdk.CfnOutput(this, 'ApiUrl', { value: `https://${this.stackName}.execute-api.${this.region}.amazonaws.com/prod/` });
        new cdk.CfnOutput(this, 'GuardrailId', { value: guardrail.attrGuardrailId });
        new cdk.CfnOutput(this, 'VectorBucketName', { value: `benefits-vectors-${this.account}` });
    }
}