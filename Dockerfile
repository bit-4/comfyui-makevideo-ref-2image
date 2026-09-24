# clean base image containing only comfyui, comfy-cli and comfyui-manager
FROM runpod/worker-comfyui:5.10.0-base

# build-time tokens for gated downloads are read from BuildKit secret
# mounts — they are never written to a layer or to image history.
# pass via: docker buildx build --secret id=hf_token,env=HF_TOKEN .

COPY handler.py /handler.py
COPY workflow_api.json /workflow_api.json

ENV WORKFLOW_PATH=/workflow_api.json
ENV NETWORK_VOLUME_DEBUG=true

# download models into comfyui
RUN --mount=type=secret,id=hf_token BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)" comfy model download --url 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors' --relative-path models/diffusion_models --filename 'minimax_h3_fl2va_pruned_int8_convrot.safetensors' && break; if [ $i -eq 5 ]; then echo "model-download failed after 5 attempts" >&2; exit 1; fi; SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && echo "model-download attempt $i failed; retrying in $SLEEP seconds" >&2; sleep $SLEEP; done
RUN --mount=type=secret,id=hf_token BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)" comfy model download --url 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors' --relative-path models/text_encoders --filename 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors' && break; if [ $i -eq 5 ]; then echo "model-download failed after 5 attempts" >&2; exit 1; fi; SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && echo "model-download attempt $i failed; retrying in $SLEEP seconds" >&2; sleep $SLEEP; done
RUN --mount=type=secret,id=hf_token BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)" comfy model download --url 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors' --relative-path models/vae --filename 'minimax_h3_video_vae_fp16.safetensors' && break; if [ $i -eq 5 ]; then echo "model-download failed after 5 attempts" >&2; exit 1; fi; SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && echo "model-download attempt $i failed; retrying in $SLEEP seconds" >&2; sleep $SLEEP; done
RUN --mount=type=secret,id=hf_token BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)" comfy model download --url 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors' --relative-path models/vae --filename 'minimax_h3_audio_vae_fp32.safetensors' && break; if [ $i -eq 5 ]; then echo "model-download failed after 5 attempts" >&2; exit 1; fi; SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && echo "model-download attempt $i failed; retrying in $SLEEP seconds" >&2; sleep $SLEEP; done
RUN --mount=type=secret,id=hf_token BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)" comfy model download --url 'https://huggingface.co/lightx2v/Minimax-h3-Turbo/resolve/main/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors' --relative-path models/loras --filename 'minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors' && break; if [ $i -eq 5 ]; then echo "model-download failed after 5 attempts" >&2; exit 1; fi; SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && echo "model-download attempt $i failed; retrying in $SLEEP seconds" >&2; sleep $SLEEP; done

# copy all input data (like images or videos) into comfyui (uncomment and adjust if needed)
# COPY input/ /comfyui/input/

# user-provided inputs override the auto-generated placeholders above.
RUN wget --progress=dot:giga -O '/comfyui/input/test004.jpg' "https://cool-anteater-319.convex.cloud/api/storage/f7eaecae-cea3-4cc4-9f00-cf0015d2b052"
RUN wget --progress=dot:giga -O '/comfyui/input/Forest_rusty_pebbles_lake-scenery_HD_wallpaper_1920x1200.jpg' "https://cool-anteater-319.convex.cloud/api/storage/d906a23a-44bd-4f93-aaa0-07962e90180e"
