# Aether Amazon Bedrock Provider

Official Aether provider plugin for Amazon Bedrock. It adds Bedrock model discovery, text generation, embeddings, and guardrail-aware provider routing for agents that use AWS-hosted models.

Install from Aether:

```bash
aether plugins install @aether/amazon-bedrock-provider
```

Configure AWS credentials and region through your normal Aether credential/profile setup, then select Bedrock models with the `amazon-bedrock/...` provider prefix.
