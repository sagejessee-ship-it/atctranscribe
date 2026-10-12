# vLLM with its audio extra (ADR-025).
#
# The official vllm/vllm-openai image leaves out vllm[audio]: av, scipy, soundfile, soxr
# and mistral-common[audio]. Audio LLMs need it to decode input_audio. Without it Voxtral
# stops at start with "`soundfile` is not installed". This adds exactly that extra, at the
# image's own vLLM version, and nothing else.
ARG VLLM_VERSION=v0.31.0
FROM vllm/vllm-openai:${VLLM_VERSION}
ARG VLLM_VERSION
RUN uv pip install --system --no-cache "vllm[audio]==${VLLM_VERSION#v}"
