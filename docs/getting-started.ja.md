<!-- i18n-source: docs/getting-started.md@blob:efd0a7016573e1bf3f1c11e41a5d2bde88159807 -->

# はじめに

> **対象: CPersona {{ version_line }}**。インストールとセットアップの正式な手順はこのページです。
> README は PyPI のプロジェクトページも兼ねるので、同じ手順の短縮版を載せています。
> 食い違う場合はこのページが優先されます。
>
> **翻訳について**: 原文は英語版です。日本語版が古いときは英語版を参照してください。

CPersona は [MCP](https://modelcontextprotocol.io/) サーバーです。インストールして
MCP クライアントの接続先にすれば、クライアントのエージェントは、セッションをまたいで
残る記憶を扱う `store` / `recall` ツールを使えるようになります。それ以外の構成は何も変わりません。

## エージェントに任せる { #let-your-agent-set-it-up }

Claude Code か Codex に、[公式サイト](https://cpersona.cloto.dev/ja/#setup)のセットアップ用プロンプトを貼り付けてください。プロンプトはエージェントにこのガイドを読ませ、各手順を先に見せ、コマンドの実行・インストール・ファイルの変更の前にあなたの承認を待つよう指示します。始めるのに必要なのはエージェントだけです。下の手順に要るもの (Python 3.11 以上や uv など) はエージェントが提案し、承認するまで何もインストールしません。

このページの残りは、同じセットアップを自分で進めるための手順です。

## 前提条件 { #prerequisites }

自分で進める場合に必要なものは次のとおりです。

- **Python 3.11 以上**
- ワンコマンド経路を使うなら **[uv](https://docs.astral.sh/uv/)** (任意。`pip` でも可)
- MCP クライアント: Claude Desktop、Claude Code、Codex CLI、Cursor、VS Code、
  またはその他の MCP ホスト (手順 3 に各クライアントのエントリがあります)

## エージェント用のプラグインを入れる (Claude Code と Codex) { #let-the-agent-do-it-claude-code }

このリポジトリは Claude Code と Codex のプラグインの配布元 (marketplace) も兼ねています。
配布しているプラグインは `cpersona` の 1 つです。中身は 2 つあります。1 つは
[Agent Skill](https://github.com/Cloto-dev/cpersona/tree/master/plugin/skills/cpersona-memory)
で、エージェントにインストール手順を案内し、その後 *いつ* store / recall / archive すべきかを教えます。
もう 1 つは、エージェントが忘れがちな時点で知らせる 3 つのフックです (下の表)。サーバー本体は
プラグインに入っていません。いつもどおり手順 3 でつなぐので、どの方法で入れてもツールの名前は同じです。

Claude Code (2.1.275 以降) では、次の 1 行で入ります。

```text
/plugin install cpersona --marketplace Cloto-dev/cpersona
```

Codex では、次の 2 行です。

```bash
codex plugin marketplace add Cloto-dev/cpersona
codex plugin add cpersona@cpersona
```

Codex はプラグインのフックを信頼するまで動かしません。入れた後に Codex の画面で `/hooks` を
開いて信頼してください。フックが変わる更新の後にも、もう一度必要です。`codex exec` は確認なしで動かします。

あとはエージェントに *「CPersona をセットアップして。永続記憶がほしい」* と言うだけです。

どのフックもエージェントの文脈に知らせを 1 つ足すだけです。エージェントを止めることも、
サーバーと通信することもありません。入力を読めない時は何も足しません。

| いつ | 知らせの中身 | 知らせの長さ |
| --- | --- | --- |
| セッションの開始 (新規、または `/clear` の後) | 最初の作業の前に `reconstruct` を呼ぶこと。残す価値のあるものは、終わりにまとめずその場で `store` すること | 約 410 字、1 回 |
| 発言のたび | この依頼について `reconstruct` を呼ぶこと (発言を問いの候補として添えます)。「その記録はありません」は完全一致の検索で確かめること | 約 310 字と発言の先頭 200 字まで、発言ごと |
| 会話が圧縮された後 | 進行中の作業について `reconstruct` を呼ぶこと。要約に書かれたことは、事実として保存する前に確かめること | 約 410 字、圧縮ごと |

圧縮の直前に動くフックはありません。どちらの製品でも、そのフックはエージェントに文を渡せないためです。
記憶を新しく保つのは、作業しながら少しずつ保存することです。フックを止めるには、エージェントが動く
環境で `CPERSONA_HOOKS_DISABLE` に `session-start`、`prompt`、`compact` をカンマ区切りで並べるか、
`all` を指定します。

自分で登録したフック (たとえば以前の `examples/claude-code-hooks/prompt_hook.py` の写し)、自作のプラグイン、
写した skill で CPersona を促している場合、このプラグインはそれを置き換えません。両方が動くので、
エージェントは同じ知らせを 2 回読み、skill も 2 つ見えます (Claude Code ではプラグインの方が
`cpersona:cpersona-memory` という名前になります)。どちらか 1 つにしてください。プラグインに移るなら、
`.claude/settings.json` または Codex のフックの設定から自分のフックの登録を外し、写した skill のフォルダー
(`~/.claude/skills/cpersona-memory` または `~/.codex/skills/cpersona-memory`) を消します。自分のものを
残すなら、重なるプラグインのフックを `CPERSONA_HOOKS_DISABLE` で止めます。ツールの名前 (`mcp__cpersona__*`)
を使うフックや設定は、どちらでもそのまま動きます。プラグインはサーバーを含まないので、ツールの名前は変わりません。

skill だけなら、フックなしでも配っています。ほかのクライアントで使う時や、手で写す時に使ってください。

```bash
# PyPI からインストール済みなら、skill は wheel の中にあります (clone 不要):
python -c "import cpersona,pathlib,shutil; s=pathlib.Path(cpersona.__file__).parent/'skills'/'cpersona-memory'; shutil.copytree(s, pathlib.Path.home()/'.claude/skills/cpersona-memory', dirs_exist_ok=True)"

# uvx (隔離環境) で動かしている、またはまだ入れていない場合:
git clone --depth 1 https://github.com/Cloto-dev/cpersona.git /tmp/cpersona
mkdir -p ~/.claude/skills && cp -r /tmp/cpersona/plugin/skills/cpersona-memory ~/.claude/skills/
```

以下の手動手順は、それ以外のクライアント向け、および手で設定したい人向けです。
手順 5 は、本来は skill が代行する部分です。記憶のトリガーを、頼まれなくても発火させる
ための設定にあたります。

## 1. CPersona をインストールする { #1-install-cpersona }

```bash
uvx cpersona          # インストール不要、直接実行
# または
pip install cpersona  # 以後 `cpersona` コマンドが PATH に入ります
```

<details>
<summary>ソースから (開発用)</summary>

```bash
git clone https://github.com/Cloto-dev/cpersona.git
cd cpersona
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install .
```

`python -m cpersona` (または `python server.py`) で実行します。
</details>

<details>
<summary>コンテナで動かす</summary>

```bash
git clone https://github.com/Cloto-dev/cpersona.git
cd cpersona
docker build -t cpersona .

docker volume create cpersona-data
docker run -d --name cpersona -p 8402:8402 \
  -e CPERSONA_AUTH_TOKEN="$(openssl rand -hex 32)" \
  -v cpersona-data:/data cpersona
```

イメージは公開していないので、ビルドは手元で行ってください。動かす前に知っておく
ことが 3 点あります。

- **このイメージが提供するのは Streamable HTTP トランスポート** (8402) です。
  MCP クライアントがサブプロセスとして起動する stdio の形にするには、`-i` を付けて
  明示します: `docker run -i --rm -e CPERSONA_TRANSPORT=stdio -v
  cpersona-data:/data cpersona`。
- **`CPERSONA_AUTH_TOKEN` が無いと起動しません**。公開したコンテナポートは、
  プロセスがバインドした先へそのまま転送します。コンテナ内でバインドしていても、
  コンテナ外から届かないことにはなりません。
- **記憶はコンテナではなくボリュームに保存されます**。データベースの置き場所は `/data` です。
  ここに何もマウントしないと、コンテナを置き換えた時点で記憶はすべて失われます。
  名前付きボリューム (上記) はそのまま動きます。ホストのディレクトリを bind mount
  する場合は所有権が引き継がれないので、uid `10001` が書ける状態にするか、
  `--user "$(id -u)"` を渡してください。

embedding backend を用意するまで、recall は keyword/FTS だけで動きます。次の節を参照し、
用意できたら `-e CPERSONA_EMBEDDING_MODE=http -e
CPERSONA_EMBEDDING_URL=http://<host>:8401/embed` を渡してください。
</details>

<details>
<summary>embedding サーバーごとコンテナで動かす</summary>

このリポジトリの `compose.yaml` は CPersona と CEmbedding を互いに繋いだ状態で
起動します。ベクトル検索を使うのに 2 つ目のセットアップは要りません。

```bash
export CPERSONA_AUTH_TOKEN=$(openssl rand -hex 32)
docker compose run --rm embedding cembedding-download-model --model jina-v5-nano
docker compose up -d
```

真ん中の行は飛ばさないでください。これが無いと、embedding サーバーは最初の
リクエストで重み (このモデルで約 800 MB) を取りに行きます。その間ポートは接続を
受け付けますが、応答はできません。先にボリュームへダウンロードしておけば、原因の分からない
タイムアウトではなく、待っていればよい 1 つの手順になります。

embedding のポートは公開しません。CPersona は compose が作るネットワーク越しに
接続し、それ以外からつなぐ必要はありません。イメージはどちらも手元でビルドし、
embedding サーバーはリビジョンで固定してあるので、後日ビルドしても同じ組み合わせになります。

記憶は `cpersona-data` ボリュームに、モデルは `embedding-model` に保存されます。
`docker compose down` はどちらも残し、`down -v` は消します。
</details>

サーバーは起動時に pypi.org へ新しいリリースの有無を問い合わせます。結果は `recall`
と `check_health` を通じて呼び出し側のエージェントに伝わり、`check_update` でも
照会できます。確認自体を止めるには `CPERSONA_UPDATE_CHECK=false` を設定します。
更新が自動で行われることはありません。明示的な `check_update(apply=true)` と再起動が
必要です。

## 2. 埋め込みサーバーを立てる (推奨) { #2-set-up-an-embedding-server-recommended }

契約を満たす埋め込みサーバーに接続してください。埋め込みバックエンド無しでの実行は
fallback としてサポートされますが、通常の運用では推奨しません。

ベクトル検索は 3 つの検索層の中で最も強く、外部プロセスを必要とする唯一の層です。
無くても CPersona は FTS5 + キーワード検索で動き、
[その旨を毎回の recall で伝えます](operations.md#detecting-a-dead-embedding-server)。

CEmbedding は参照実装のバックエンドです。下記の契約を満たす他の埋め込みサーバーも
同等にサポートされます。どれを選ぶかは利用者の自由です。

### 契約 { #the-contract }

CPersona は埋め込みサーバーを選びません。`CPERSONA_EMBEDDING_URL` を、次を実装する
任意の HTTP エンドポイントに向けてください。

```
POST /embed
リクエスト: { "texts": ["string", ...] }        # 空でない配列
レスポンス: { "embeddings": [[float, ...], ...], "dimensions": <int> }
```

CPersona が読むのは **`embeddings`** だけです。`dimensions` はリファレンス
サーバーの応答に含まれますが、クライアントは無視します。返さないバックエンドでも
動きます。CPersona が 1 リクエストで送るのは最大 **64 件** (記録の文の区切りを作る時。
それ以外は 32 件) で、リファレンス実装が受け付ける上限は 100 件です。この範囲のバッチ上限は考慮する必要がありません。

見落としやすく、いずれも**気づかないうちにランキングを劣化させる**要件が 3 つあります。

- **埋め込みは L2 正規化されていなければなりません**。CPersona は類似度をそのままの
  内積で計算するため、正規化されていないベクトルを返すバックエンドは、ベクトルの
  大きさでランキングを歪めます。サポート対象のバックエンド (クライアントの `api`
  モードと全 CEmbedding プロバイダ) はすべて正規化済みです。
- **契約はロールを持ちません**。クエリと文書は同じ呼び出しを通り、指示
  プレフィックスは付きません。プレフィックス前提のモデル (e5 系、prompted bge) は
  この契約の下では性能が出ません。対称型または retrieval 統合型のモデル
  (jina-v5-nano、bge-m3、MiniLM) が、この契約に合うと想定しているモデルです。
- **同じ URL の裏でモデルを差し替えるとコーパスが無効になります**。`/embed` の
  契約は、モデルが同じかどうかの情報を含みません。`GET /capabilities` にも答えるバックエンド
  (CEmbedding 0.9.0 以降) はフィンガープリントでモデルを示し、2.6.3 からは保存するベクトルに
  そのフィンガープリントのラベルが付くので、別のモデルが書いたベクトルを `check_health` が
  数えます (`embedding_model_mismatch`)。その報告が無いバックエンド (古いバージョンや別の
  実装) では、CPersona が差し替えに気づけるのは埋め込みの*次元*が変わったときだけで、
  同じ次元の別モデルへの差し替えは検出できません。2.6.3 より前に保存したベクトルには
  ラベルが無く、数えられません。

  気づけても修復はされません。`check_health(fix=true)` が再埋め込みするのは
  blob が NULL の行で、次元チェックは*長さ*の違う blob だけを NULL 化します。
  同一次元での差し替え後は全 blob が期待どおりの大きさなので、何も NULL 化されず、
  何も再埋め込みされません。既に blob を持つ行を強制的に再埋め込みするツールは
  ありません。コーパスを再構築するまでの間は、`CPERSONA_EMBEDDING_MODEL_MODE=reject`
  で古いベクトルを問いと比べないようにできます。そのとき該当する行はキーワードでだけ
  見つかります。

  復旧手段はコーパスの再構築です。`delete_agent_data` の後、
  [再構築パターン](operations.md#corpus-indexing-and-sync-patterns) のとおりに再
  `store` し、最後に `calibrate_threshold` を実行してください。

### リファレンス実装 { #the-reference-server }

[CEmbedding](https://github.com/Cloto-dev/CEmbedding) (MIT) は jina-v5-nano を
オンデバイス (CPU) で動かし、このとおりのエンドポイントを公開します。

```bash
# モデルを ./data/models にダウンロード
uvx --from "cembedding[onnx]" cembedding-download-model --model jina-v5-nano

# サーバーを起動 (カレントディレクトリの ./data/models を読みます)
EMBEDDING_PROVIDER=onnx_jina_v5_nano uvx --from "cembedding[onnx]" cembedding
```

`pip install "cembedding[onnx]"` で PATH に入れて
`cembedding-download-model --model jina-v5-nano` → `cembedding` としても同じです。
ソースチェックアウトからなら、同じ 2 手順は
`python -m cembedding.download_model --model jina-v5-nano` と
`python -m cembedding` です。

いずれの場合も
`HTTP embedding endpoint started on http://127.0.0.1:8401/embed`
と表示されるはずです。CPersona をつなぐ前に確認してください。

```bash
curl -s http://127.0.0.1:8401/embed \
  -H 'content-type: application/json' \
  -d '{"texts":["hello world"]}' | head -c 200
```

CPersona の既定値は jina-v5-nano (768 次元) に合わせて調整されています。契約を
満たす他のサーバーでも動きます。実測値が公開されているモデルは
[`benchmarks/`](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md)
にあります。

CPersona が必要とするのは URL だけです。ただし、**参照サーバーをどう監視下で動かすか
は重要**で、そのまま起動するやり方では動きません。

これはただの HTTP プロセスではなく MCP サーバーです。既定のトランスポートでは
フォアグラウンドで stdio の MCP セッションを動かし、REST `/embed` エンドポイントはバックグラウンド
タスクとして提供します。つまり、**プロセスがいつまで動くかは stdin で決まります**。EOF で
セッションが終わり、`finally` 節が HTTP タスクを cancel します。

サービスマネージャは stdin を `/dev/null` にしてプロセスを起動します。その形で
立ち上げると、サーバーはポートを bind し `HTTP embedding endpoint started` を
ログに出したうえで、**同じ秒のうちに終了コード 0 で終了します**。監視する側は正常終了と
判断し、CPersona は誰も応答しない URL を向いたまま残ります。

stdin を開いたままにしてください。サービスマネージャの下では、パイプを開いたままにするものを
間に挟みます (`ExecStart=/bin/sh -c 'sleep infinity | cembedding'`)。ターミナルでは端末が
既にその役割を果たしています。

`EMBEDDING_TRANSPORT=streamable-http` は stdin を読まないので、監視下でもそのまま
動きます。ただし REST `/embed` の**代わりに** MCP エンドポイントを提供するため、
`/embed` に POST する CPersona の `http` モードでは選択肢になりません。

## 3. MCP クライアントに登録する { #3-register-cpersona-with-your-mcp-client }

どのクライアントも起動するプロセスは同じで、コマンド `uvx`、引数 `cpersona`、
下に示す環境変数です。違うのは、読むファイルとキーの名前だけです。

先に絶対パスの `CPERSONA_DB_PATH` を決めてください。例が
`/home/you/.claude/cpersona.db` を使っているのは、Claude 利用者ならそのディレクトリが
既にあるからです。絶対パスなら何でも構いません。

| クライアント | エントリの置き場所 | 形 | ここで確認 |
| --- | --- | --- | --- |
| Claude Code | `claude mcp add-json … -s user` (ユーザー設定に書く) | JSON、`type: stdio` | 済 |
| Claude Desktop | `claude_desktop_config.json` | JSON、`mcpServers` | 済 |
| Codex CLI | `codex mcp add … -- uvx cpersona` (`~/.codex/config.toml` に書く) | TOML、`[mcp_servers.cpersona]` | 済 (codex-cli 0.147.0) |
| Cursor | `~/.cursor/mcp.json` (グローバル) または `.cursor/mcp.json` (プロジェクト) | JSON、`mcpServers` | ベンダー docs |
| VS Code (Copilot) | `.vscode/mcp.json` (ワークスペース) またはユーザーの `mcp.json` | JSON、`servers` | ベンダー docs |
| その他の MCP ホスト | その stdio サーバー設定 | 同じ command / args / env の 3 つ組 | ベンダー docs |

「済」は、このページを書いた時点で、メンテナのマシン上でそのクライアント自身のツールを使ってエントリを書き、
読み戻して確認したことを示します。「ベンダーdocs」はクライアントのドキュメントから形を転記したもので、
ここでは実行していません。あなたのクライアントが受け付けるものと食い違ったら、正しいのはクライアントのほうです。
報告してください。

**Claude Desktop**: `claude_desktop_config.json` に次を追加します。

```json
{
  "mcpServers": {
    "cpersona": {
      "command": "uvx",
      "args": ["cpersona"],
      "env": {
        "CPERSONA_DB_PATH": "/home/you/.claude/cpersona.db",
        "EMBEDDING_MODE": "http",
        "EMBEDDING_HTTP_URL": "http://127.0.0.1:8401/embed"
      }
    }
  }
}
```

**Claude Code**: 1 コマンドで登録します。
{ #register-claude-code }

`/home/you` は自分のホームディレクトリに書き換えてください (Windows では `C:/Users/you`)。埋め込みサーバーをまだ用意していない場合は ([手順 2](#2-set-up-an-embedding-server-recommended))、`"EMBEDDING_MODE":"http"` を `"EMBEDDING_MODE":"none"` に変えてください。

```bash
claude mcp add-json cpersona '{"type":"stdio","command":"uvx","args":["cpersona"],"env":{"CPERSONA_DB_PATH":"/home/you/.claude/cpersona.db","EMBEDDING_MODE":"http","EMBEDDING_HTTP_URL":"http://127.0.0.1:8401/embed"}}' -s user
```

**Codex CLI**: 1 コマンドで登録します。このコマンドが、その後に示す TOML を `~/.codex/config.toml` に書き込みます。
{ #register-codex }

`/home/you` は自分のホームディレクトリに書き換えてください (Windows では `C:/Users/you`)。埋め込みサーバーをまだ用意していない場合は ([手順 2](#2-set-up-an-embedding-server-recommended))、`EMBEDDING_MODE=http` を `EMBEDDING_MODE=none` に変えてください。

```bash
codex mcp add cpersona --env CPERSONA_DB_PATH=/home/you/.claude/cpersona.db --env EMBEDDING_MODE=http --env EMBEDDING_HTTP_URL=http://127.0.0.1:8401/embed -- uvx cpersona
```

```toml
[mcp_servers.cpersona]
command = "uvx"
args = ["cpersona"]

[mcp_servers.cpersona.env]
CPERSONA_DB_PATH = "/home/you/.claude/cpersona.db"
EMBEDDING_HTTP_URL = "http://127.0.0.1:8401/embed"
EMBEDDING_MODE = "http"
```

Codex はサーバーごとにツールを拒否リストにできます (同じテーブルの
`disabled_tools = ["delete_memory", …]`)。これはエージェントに読み取り中心の
アクセスを渡す、クライアント側の方法です。サーバー側で全クライアントに一度に効く同じ働きのものは
[クライアント別ケイパビリティ層](ACL_DESIGN.md) です。

**Cursor**: `~/.cursor/mcp.json`、またはプロジェクト内の `.cursor/mcp.json` に書きます。
形は Claude Desktop と同じです。

```json
{
  "mcpServers": {
    "cpersona": {
      "command": "uvx",
      "args": ["cpersona"],
      "env": {
        "CPERSONA_DB_PATH": "/home/you/.claude/cpersona.db",
        "EMBEDDING_MODE": "http",
        "EMBEDDING_HTTP_URL": "http://127.0.0.1:8401/embed"
      }
    }
  }
}
```

**VS Code (Copilot)**: ワークスペースの `.vscode/mcp.json`、またはユーザーレベルの
`mcp.json` (*MCP: Open User Configuration*) に書きます。トップレベルのキーは `mcpServers`
ではなく `servers` です。

```json
{
  "servers": {
    "cpersona": {
      "command": "uvx",
      "args": ["cpersona"],
      "env": {
        "CPERSONA_DB_PATH": "/home/you/.claude/cpersona.db",
        "EMBEDDING_MODE": "http",
        "EMBEDDING_HTTP_URL": "http://127.0.0.1:8401/embed"
      }
    }
  }
}
```

セットアップでつまずく原因は、たいてい次の 3 点です。

- **`CPERSONA_DB_PATH` は絶対パスにしてください**。既定値 `data/cpersona.db` は
  *クライアントの*作業ディレクトリからの相対です。別の場所から起動された
  クライアントは、別の空のデータベースを開きます。Windows では
  `C:/Users/you/.claude/cpersona.db` の形で書きます。
- **まだ埋め込みサーバーが無い?** `EMBEDDING_*` の 2 行を消す (または
  `EMBEDDING_MODE=none` を設定する) だけです。CPersona は FTS5 + キーワードで
  動作し、劣化している旨を報告します。
- `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` は `CPERSONA_EMBEDDING_MODE` /
  `CPERSONA_EMBEDDING_URL` の汎用エイリアスです。両方設定された場合は接頭辞つきが
  優先されます。[設定リファレンス](configuration.md) は、サーバーが読む変数を
  既定値とともにすべて載せています。

## 4. 動作を確認する { #4-verify-it-works }

エージェントに何かを保存させ、*新しい*セッションでそれを想起させてください。
セッションの境界を越えることこそが目的です。

> 「これを保存して: デプロイ手順は ops/deploy.md にある」
>
> …そして新しいセッションで: 「デプロイ手順について何か言っていたよね?」

コーパスが実運用に入ったら、一度は走らせておきたい確認が 2 つあります。

- `check_health`: 登録された検査の一覧 (レジストリ) に沿ったヘルスチェック。判定は `status`、検出項目は
  重大度つき (`critical` / `warn` / `info`) で、`check_health(fix=true)` が機械的な
  ものを修復します。
- recall 応答に `advisory` フィールドが出ていないか。これはベクトル検索が寄与
  していないという報告です。理由は重大度で区別します。`hint` は埋め込みが未設定
  (`mode=none`) であること、fault は設定済みのエンドポイントが応答しなくなった
  ことを意味します。
  [埋め込みサーバーの死活検知](operations.md#detecting-a-dead-embedding-server)
  を参照してください。

## 5. 記憶のトリガーを毎セッション発火させる { #5-make-the-memory-triggers-fire-in-every-session }

登録すればエージェントはツールを持ちますが、頼まれなくても*使う*ようにはなりません。
セッション開始時の recall、決定時の store、セッション終了時の archive は、
会話がたまたま一致したときだけ発火する skill ではなく、クライアントが**毎**セッション
読み込むファイルの中に置く必要があります。どのクライアントにも、そのファイルが
あります。

| クライアント | 常時ロードされるファイル (ユーザーレベルの既定) | プロジェクトレベルの代替 |
| --- | --- | --- |
| Claude Code / Claude Desktop | `~/.claude/CLAUDE.md` | `./CLAUDE.md` |
| Codex CLI | `~/.codex/AGENTS.md` | リポジトリルートの `./AGENTS.md` |
| Cursor | User Rules (*Customize → Rules* — ファイルではなく設定) | `alwaysApply: true` の `.cursor/rules/cpersona.mdc`、または `./AGENTS.md` |
| VS Code (Copilot) | クライアントの custom-instructions ドキュメントを参照 | `.github/copilot-instructions.md`、または `chat.useAgentsMdFile` を有効にした `./AGENTS.md` |

下のブロックをそのファイルに貼り付けてください。`<AGENT_ID>` は、エージェントが
毎回の呼び出しで使う安定した識別子 1 つ (`"claude-code"`、`"codex"`、…) に
置き換えます。

マーカーは残してください。後のバージョンのブロックは、これを手がかりに 2 つ目のコピーを
積み重ねるのではなく、このブロックを見つけて置き換えます。Claude Code では
`cpersona-memory` skill が承認を得て貼り付けを代行します。それ以外では手で
貼り付けます。

ブロックが従う規則 (同意、配置、冪等性、40 行の予算、クライアント中立) は
[ポリシーブロック標準](CLAUDE_MD_POLICY_STANDARD.md) にあります。ブロックの原本は
skill 側にあり、このコピーは CI でそれと照合されます。

```markdown
<!-- BEGIN cpersona-policy v5 (managed by the cpersona-memory skill; re-run the skill to update) -->
## CPersona memory policy

Use the CPersona MCP tools proactively with `agent_id="<AGENT_ID>"` — never wait to be asked.

**Session start** → `reconstruct(agent_id, query="<opening-topic keywords>")` before the first
substantive action; with no topic yet, `recall(agent_id, query="", limit=10)` for the latest.
`deep=true` when the first pass comes back thin. Skip only for trivial one-shot questions.

**Answering from memory** → `reconstruct(agent_id, query)`, not `recall`: 10 items by default, each
quoting the rows behind it, at fewer reader tokens per correct answer. Read an item to judge it.

**Decisions, rules, preferences, bug findings** → `store` immediately. Fire on phrases like
"let's go with X", "from now on always Y", "remember that…", "approved", "that's a bug".
Lock must-never-lose rules: `lock: true` on the `store` where it offers one, else `lock_memory`.
After a successful `git commit`, `store` a one-line record: hash, what changed, why.

**Changing an existing rule** → `update_memory`, never delete + store. If the memory is locked:
`unlock_memory` → `update_memory` → `lock_memory`.

**Session end** — fire on closing phrases ("that's all for today", "wrap it up", "good night") →
first `store` + lock any unsaved decisions, then `archive_episode(agent_id, history=<the REAL
turns>, summary=…, keywords=…, resolved=…)`, computing `summary` and `keywords` yourself.

**"Don't save this" / benchmark sessions** → `pause_persistence(ttl_seconds=1800)`;
`resume_persistence()` (or TTL expiry) restores. Reads still answer, minus the writes inside them.

**Degraded mode** — if a `recall` response carries an `advisory` field, surface it to the user
and follow its runbook. Never quietly serve keyword-only recall.

**Quality** — recall feels off → `set_recall_precision` (strict/balanced/lenient), the one policy
knob; after large corpus changes `calibrate_threshold(agent_id)`; monthly `check_health(agent_id, fix=true)`.

**If this client keeps a memory file that loads every session** (Claude Code's `MEMORY.md`), use it
as the deterministic index over this store: one line per memory — `- <slug> — <the sentence that
changes behaviour>` — with the body stored here under the id `memory-index:<slug>` and content
starting `[<slug>]`, so a line tells you what to `recall`. Recall is ranked and may not surface a
memory; the index always arrives. Its size cap fails **silently** when exceeded, so consolidate at
80%, not at the limit. Never migrate existing memories into this store without asking first.

Details, setup, and troubleshooting: the `cpersona-memory` skill.
<!-- END cpersona-policy -->
```

## 次に読むもの { #where-to-go-next }

| したいこと | 読むページ |
|---|---|
| 依存してよい挙動を知る | [挙動契約](behavior-contracts.md) |
| 各ツールが何をするか見る | [ツール一覧](tools.md) |
| 検索の仕組みを理解する | [アーキテクチャ](architecture.md) |
| 稼働中のインスタンスをバックアップ・調整・診断する | [運用 Runbook](operations.md) |
| 設定を調べる | [設定リファレンス](configuration.md) |
| 複数クライアントにネットワーク越しで提供する | [リモート HTTP トランスポート](configuration.md#remote-http-transport) |
