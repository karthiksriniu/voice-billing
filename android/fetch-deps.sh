#!/bin/bash
# The sherpa-onnx AAR (47 MB) and the KWS model (8 MB) are vendored prebuilts, kept out of
# git for the same reason poc/.tools is — same convention, same trade. Run once after a
# fresh clone.
set -euo pipefail
cd "$(dirname "$0")"

SHERPA=1.13.6
KWS=sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01

if [ ! -f "app/libs/sherpa-onnx-$SHERPA.aar" ]; then
  echo "fetching sherpa-onnx $SHERPA aar"
  mkdir -p app/libs
  curl -fL --retry 3 -o "app/libs/sherpa-onnx-$SHERPA.aar" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/v$SHERPA/sherpa-onnx-$SHERPA.aar"
fi

# NOTE: the "-mobile" variant of this model is NOT interchangeable. Its encoder aborts
# inside KeywordSpotter_decode with a Reshape shape mismatch (input {17,1,128} vs requested
# {8,2,1,128}) and takes the process down with it. Use the standard export.
if [ ! -f app/src/main/assets/kws/tokens.txt ]; then
  echo "fetching KWS model"
  mkdir -p app/src/main/assets/kws
  tmp=$(mktemp -d)
  curl -fL --retry 3 -o "$tmp/kws.tar.bz2" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$KWS.tar.bz2"
  tar xjf "$tmp/kws.tar.bz2" -C "$tmp"
  cp "$tmp/$KWS/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" \
     "$tmp/$KWS/decoder-epoch-12-avg-2-chunk-16-left-64.onnx" \
     "$tmp/$KWS/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx" \
     "$tmp/$KWS/tokens.txt" app/src/main/assets/kws/
  rm -rf "$tmp"
fi

# Silero, reused from the PoC so both builds endpoint against the identical model.
cp -n ../poc/public/vendor/silero_vad_v5.onnx app/src/main/assets/kws/silero_vad.onnx 2>/dev/null || true
printf '%s\n' "▁THEY ▁A C C U LA" > app/src/main/assets/kws/keywords.txt

echo "done. models:"; ls -la app/src/main/assets/kws
