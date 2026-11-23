# Sourced by the Questions runners from the repository root.
install_questions_llama() {
    local tag archive
    mkdir -p build/questions-models
    tag=$(jq -r .llama_tag "$CFG")
    archive="llama-$tag-bin-ubuntu-x64.tar.gz"
    timeout 5m gh release download "$tag" -R ggml-org/llama.cpp --pattern "$archive" --dir build/questions-models
    printf '%s  %s\n' "$(jq -r .llama_archive_sha256 "$CFG")" "build/questions-models/$archive" | sha256sum -c -
    mkdir build/questions-models/llama
    tar -xzf "build/questions-models/$archive" -C build/questions-models/llama --strip-components=1
    SERVER="$PWD/build/questions-models/llama/llama-server"
    export LD_LIBRARY_PATH="$PWD/build/questions-models/llama:${LD_LIBRARY_PATH:-}"
}

start_server() {
    local role=$1 port=$2
    local repo rev file sha alias
    repo=$(jq -r ".${role}_repo" "$CFG")
    rev=$(jq -r ".${role}_hf_revision" "$CFG")
    file=$(jq -r ".${role}_file" "$CFG")
    sha=$(jq -r ".${role}_sha256" "$CFG")
    alias=$(jq -r ".${role}_model" "$CFG")
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
        echo "::error::Questions model port already occupied"; exit 1
    fi
    timeout 15m curl -fsSL --retry 2 --connect-timeout 30 --max-time 600 \
        "https://huggingface.co/$repo/resolve/$rev/$file" -o "build/questions-models/$role.gguf"
    printf '%s  %s\n' "$sha" "build/questions-models/$role.gguf" | sha256sum -c -
    env -u OPENAI_API_KEY -u DB_KEY -u GH_TOKEN "$SERVER" \
        -m "build/questions-models/$role.gguf" --alias "$alias" \
        --host 127.0.0.1 --port "$port" -c 16384 -np 1 -t 4 -fa on --jinja \
        --cache-ram 0 --offline --no-webui --log-disable > "build/questions-models/$role.log" 2>&1 &
    PID=$!
    for _ in $(seq 1 120); do
        kill -0 "$PID" 2>/dev/null || { echo "::error::Local llama-server exited (private log retained locally)"; exit 1; }
        if curl -fsS --max-time 3 "http://127.0.0.1:$port/health" | grep -q ok; then return; fi
        sleep 5
    done
    echo "::error::Local llama-server readiness timeout"; exit 1
}

stop_server() {
    if [ -n "$PID" ]; then
        kill "$PID" 2>/dev/null || true
        wait "$PID" 2>/dev/null || true
        PID=""
    fi
}
