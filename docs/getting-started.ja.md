<!-- i18n-source: docs/getting-started.md@blob:75db6129f98b2f4ab3ef1db3bbdd96d4e5d913fc -->

# はじめに

> **対象: CPersona 2.5.x。** このページがインストールとセットアップの正本です。
> README は PyPI のプロジェクトページも兼ねるため同じ手順の短縮版を持ちます。
> 食い違う場合はこのページが優先されます。
>
> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

CPersona は [MCP](https://modelcontextprotocol.io/) サーバーです。インストールして
MCP クライアントを向ければ、クライアントのエージェントはセッションをまたいで
生き残る `store` / `recall` ツールを得ます。それ以外のスタックは何も変わりません。

## 前提条件 { #prerequisites }

- **Python 3.11 以上**
- ワンコマンド経路を使うなら **[uv](https://docs.astral.sh/uv/)** (任意 — `pip` でも可)
- MCP クライアント: Claude Desktop、Claude Code、その他の MCP ホスト

## エージェントに任せる (Claude Code) { #let-the-agent-do-it-claude-code }

このリポジトリ — および公開されている wheel — には
[Agent Skill](https://github.com/Cloto-dev/cpersona/tree/master/skills/cpersona-memory)
が同梱されています。インストール全体を Claude Code に案内させるだけでなく、
より重要なこととして、その後 *いつ* store / recall / archive すべきかを教えます。
skill を入れるのが最短経路です:

```bash
# PyPI からインストール済みなら、skill は wheel の中にあります (clone 不要):
python -c "import cpersona,pathlib,shutil; s=pathlib.Path(cpersona.__file__).parent/'skills'/'cpersona-memory'; shutil.copytree(s, pathlib.Path.home()/'.claude/skills/cpersona-memory', dirs_exist_ok=True)"

# uvx (隔離環境) で動かしている、またはまだ入れていない場合:
git clone --depth 1 https://github.com/Cloto-dev/cpersona.git /tmp/cpersona
mkdir -p ~/.claude/skills && cp -r /tmp/cpersona/skills/cpersona-memory ~/.claude/skills/
```

あとは Claude Code にこう言うだけです: *「CPersona をセットアップして。永続記憶が
ほしい」*。以下の手動手順は、それ以外のクライアント向け、および手で設定したい人
向けです。

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

実行は `python -m cpersona` (または `python server.py`)。
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

イメージは公開していないので、ビルドはあなたの手元で行います (配布物はリポジトリ
そのものです)。動かす前に知っておく価値があるのは 3 点です:

- **このイメージが提供するのは Streamable HTTP トランスポート** (8402) です。
  MCP クライアントがサブプロセスとして起動する stdio の形にするには、`-i` を付けて
  明示します: `docker run -i --rm -e CPERSONA_TRANSPORT=stdio -v
  cpersona-data:/data cpersona`。
- **`CPERSONA_AUTH_TOKEN` が無いと起動しません。** これはイメージが厳しいのでは
  ありません — 公開したコンテナポートはプロセスがバインドした先へそのまま転送する
  ので、コンテナ内でバインドしていることは「コンテナ外から届かない」ことの根拠に
  なりません。
- **記憶はコンテナではなくボリュームに載ります。** データベースの置き場所は `/data`
  で、そこにボリュームを持たないコンテナは「置き換えると忘れる記憶サーバー」です。
  名前付きボリューム (上記) はそのまま動きます。ホストのディレクトリを bind mount
  する場合は所有権が引き継がれないので、uid `10001` が書ける状態にするか、
  `--user "$(id -u)"` を渡してください。

embedding backend を与えるまで recall は keyword/FTS のみです — 次の節を参照し、
用意できたら `-e CPERSONA_EMBEDDING_MODE=http -e
CPERSONA_EMBEDDING_URL=http://<host>:8401/embed` を渡してください。
</details>

サーバーは起動時に pypi.org へ新しいリリースの有無を問い合わせ、結果を `recall`
(および `check_health`) を通じて呼び出し側のエージェントに伝えます。必要なときは
`check_update` で照会できます。`CPERSONA_UPDATE_CHECK=false` で無効化できます。
更新が自動で行われることはありません — 明示的な `check_update(apply=true)` と
再起動が必要です。

## 2. 埋め込みサーバーを立てる (推奨) { #2-set-up-an-embedding-server-recommended }

CPersona は、契約を満たす埋め込みサーバーへの接続を強く推奨します。埋め込み
バックエンド無しでの実行はサポートされる fallback 構成ですが、通常運用では
推奨されません。

ベクトル検索は 3 つの検索層の中で最も強く、外部プロセスを必要とする唯一の層です。
無くても CPersona は動き — FTS5 + キーワード検索で — かつ
[その旨を毎回の recall で伝えます](operations.md#detecting-a-dead-embedding-server)。

CEmbedding は参照実装であり第一推奨のバックエンドです。下記の契約を満たす他の
埋め込みサーバーも同等にサポートされ、同等に推奨されます — バックエンドの選択は
利用者に委ねられています。

### 契約 { #the-contract }

CPersona は埋め込みサーバーを選びません。`CPERSONA_EMBEDDING_URL` を、次を実装する
任意の HTTP エンドポイントに向けてください:

```
POST /embed
リクエスト: { "texts": ["string", ...] }        # 空でない配列
レスポンス: { "embeddings": [[float, ...], ...], "dimensions": <int> }
```

CPersona が読むのは **`embeddings`** だけです — `dimensions` はリファレンス
サーバーの応答に含まれますがクライアントは無視するため、返さないバックエンドでも
動きます。CPersona が 1 リクエストで送るのは最大 **32 件**、リファレンス実装が
受け付ける上限は 100 件なので、この範囲のバッチ上限は考慮する必要がありません。

見落としやすく、いずれも**ランキングを静かに劣化させる**要件が 3 つあります:

- **埋め込みは L2 正規化されていなければなりません。** CPersona は類似度を素の
  内積で計算するため、正規化されていないベクトルを返すバックエンドは、ベクトルの
  大きさでランキングを歪めます。サポート対象のバックエンド (クライアントの `api`
  モードと全 CEmbedding プロバイダ) はすべて正規化済みです。
- **契約はロールを持ちません。** クエリと文書は同じ呼び出しで、指示プレフィックス
  なしに埋め込まれます。プレフィックス前提のモデル (e5 系、prompted bge) はこの
  契約の下では性能が出ません。対称型または retrieval 統合型のモデル
  (jina-v5-nano、bge-m3、MiniLM) が想定される適合先です。
- **同じ URL の裏でモデルを差し替えるとコーパスが無効化されます。** CPersona は
  バックエンドを埋め込みの*次元*だけで指紋認証します — 契約はモデル同一性を
  運びません — そのため同じ次元での差し替えは検出できません。しかもこれは修復
  ツールが届かないケースです: `check_health(fix=true)` が再埋め込みするのは blob が
  NULL の行で、次元チェックは*長さ*の違う blob だけを NULL 化します。したがって
  同一次元での差し替え後は全 blob が期待どおりの大きさであり、何も NULL 化されず、
  何も再埋め込みされません。既に blob を持つ行を強制的に再埋め込みするツールは
  ありません。復旧手段はコーパスの再構築です — `delete_agent_data` してから再
  `store` する
  [再構築パターン](operations.md#corpus-indexing-and-sync-patterns) を使い、その後に
  `calibrate_threshold` を実行してください。

### リファレンス実装 { #the-reference-server }

[CEmbedding](https://github.com/Cloto-dev/CEmbedding) (MIT) は jina-v5-nano を
オンデバイス (CPU) で動かし、まさにこのエンドポイントを公開します:

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
と表示されるはずです。CPersona を繋ぐ前に確認してください:

```bash
curl -s http://127.0.0.1:8401/embed \
  -H 'content-type: application/json' \
  -d '{"texts":["hello world"]}' | head -c 200
```

CPersona の既定値は jina-v5-nano (768 次元) に合わせて調整されています。契約を
満たす他のサーバーでも動きます。実測値が公開されているモデルは
[`benchmarks/`](https://github.com/Cloto-dev/cpersona/blob/master/benchmarks/README.md)
にあります。

CPersona が必要とするのは URL だけです — ただし**参照サーバーをどう監視下で動かすか
は重要**で、素直なやり方では動きません。

これはただの HTTP プロセスではなく MCP サーバーです。既定のトランスポートでは前景で
stdio の MCP セッションを回し、REST `/embed` エンドポイントはバックグラウンドタスク
として提供します。つまり**プロセスの寿命は stdin に縛られています**: EOF でセッション
が終わり、`finally` 節が HTTP タスクを cancel します。サービスマネージャが普通に起動
する形 — stdin が `/dev/null` — で立ち上げると、ポートを bind し
`HTTP embedding endpoint started` をログに出したうえで、**同じ秒のうちに終了コード 0
で終了します**。監視側は正常終了を見て、CPersona は誰も応答しない URL を向いたまま
残ります。

stdin を開いたままにしてください。サービスマネージャの下ではパイプを保持する何かを
挟むことになります (例:
`ExecStart=/bin/sh -c 'sleep infinity | cembedding'`)。ターミナルでは端末が既にその
役割を果たしています。

`EMBEDDING_TRANSPORT=streamable-http` は stdin を読まないという意味では監視に向いた
代替ですが、REST `/embed` の**代わりに** MCP エンドポイントを提供します — したがって
`/embed` に POST する CPersona の `http` モードでは選択肢になりません。

## 3. MCP クライアントに登録する { #3-register-cpersona-with-your-mcp-client }

**Claude Desktop** — `claude_desktop_config.json` に追加:

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

**Claude Code** — 1 コマンド:

```bash
claude mcp add-json cpersona '{"type":"stdio","command":"uvx","args":["cpersona"],"env":{"CPERSONA_DB_PATH":"/home/you/.claude/cpersona.db","EMBEDDING_MODE":"http","EMBEDDING_HTTP_URL":"http://127.0.0.1:8401/embed"}}' -s user
```

問い合わせ 1 往復を節約できる注意点:

- **`CPERSONA_DB_PATH` は絶対パスにしてください。** 既定値
  `data/cpersona.db` は*クライアントの*作業ディレクトリからの相対です —
  別の場所から起動されたクライアントは、別の空のデータベースを開きます。
  Windows では `C:/Users/you/.claude/cpersona.db` の形で書きます。
- **まだ埋め込みサーバーが無い?** `EMBEDDING_*` の 2 行を消す (または
  `EMBEDDING_MODE=none` を設定する) だけです。CPersona は FTS5 + キーワードで
  動作し、劣化している旨を報告します。
- `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` は `CPERSONA_EMBEDDING_MODE` /
  `CPERSONA_EMBEDDING_URL` の汎用エイリアスです。両方設定された場合は接頭辞つきが
  優先されます。[設定リファレンス](configuration.md) は手を伸ばしそうな設定を
  網羅していますが、完全な一覧ではありません — いくつかの変数
  (`CPERSONA_STORE_BLOB`、`CPERSONA_FTS_ENABLED`、`CPERSONA_EMBEDDING_API_KEY`、
  `CPERSONA_CALIBRATE_*` の 2 つ、ほか数個) はサーバーが読むにもかかわらずそこに
  載っていません。完全な一覧は `cpersona/config.py` です。

## 4. 動作を確認する { #4-verify-it-works }

エージェントに何かを保存させ、それを想起させてください — できれば*新しい*
セッションで。セッション境界を越えることこそが目的だからです:

> 「これを保存して: デプロイ手順は ops/deploy.md にある」
>
> …そして新しいセッションで: 「デプロイ手順について何か言っていたよね?」

コーパスが実運用に入ったら、一度は走らせておきたい確認が 2 つあります:

- `check_health` — レジストリ駆動のヘルスチェック。判定は `status`、検出項目は
  重大度つき (`critical` / `warn` / `info`) で、`check_health(fix=true)` が機械的な
  ものを修復します。
- recall 応答に `advisory` フィールドが出ていないか。これはベクトル検索が寄与して
  いないことの報告で、重大度が理由を区別します: `hint` は埋め込みが単に未設定
  (`mode=none`) であることを、fault は設定済みのエンドポイントが応答しなくなった
  ことを意味します —
  [埋め込みサーバーの死活検知](operations.md#detecting-a-dead-embedding-server)
  を参照してください。

## 次に読むもの { #where-to-go-next }

| したいこと | 読むページ |
|---|---|
| 依存してよい挙動を知る | [挙動契約](behavior-contracts.md) |
| 各ツールが何をするか見る | [ツール一覧](tools.md) |
| 検索の仕組みを理解する | [アーキテクチャ](architecture.md) |
| 稼働中のインスタンスをバックアップ・調整・診断する | [運用 Runbook](operations.md) |
| 設定を調べる | [設定リファレンス](configuration.md) |
| 複数クライアントにネットワーク越しで提供する | [リモート HTTP トランスポート](configuration.md#remote-http-transport) |
