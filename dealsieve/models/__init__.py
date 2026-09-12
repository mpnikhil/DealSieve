"""Model provider abstraction for Strands. Owned by W3.

get_model(purpose) returns a strands.models.Model chosen by DEALSIEVE_MODEL_BACKEND:
  cli       -> CLIModel (claude -p | codex exec | agy -p), uses local subscriptions, no API keys
  anthropic -> strands AnthropicModel
  bedrock   -> strands BedrockModel
  openai    -> strands OpenAIModel
  scripted  -> ScriptedModel replaying fixtures/scripted/*.json (deterministic tests)
"""

from dealsieve.models.backend import backend_name, get_model  # noqa: F401
