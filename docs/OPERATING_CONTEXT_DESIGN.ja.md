<!-- i18n-source: docs/OPERATING_CONTEXT_DESIGN.md@blob:e2d741baab28b4c54c304e242316d1b6d6ea3b61 -->

# サーバー供給の運用コンテキスト (グローバル設定 + MCP instructions 配布)

**ステータス**: `feature/2.5.1-operating-context` で実装済み (対象: 2.5.1、直接
リリース — pre-release ladder は踏まない。merge / tag は 2.5.0 final をゲートとする)
**決定**: プロジェクトオーナー、2026-07-16 (2.5.0b1 リリース直後の設計討議)
**範囲**: 加算的 — DB スキーマ変更なし、新規の読み取りツール 1 つ、sidecar 設定
ファイル 1 つ。`SUPPORT.md` にある 2.5.x ラインの宣言 ("DB schema and MCP tool
contract are preserved, rollback-free") は維持されます。

> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

---

## 1. 動機 { #1-motivation }

CPersona の運用ドクトリン — どの `project_id` が存在するのか、`agent_id` にどんな
規約が適用されるのか、`recall` をどう使うべきか (limit、`exclude_contents`、
セッション開始時の規律) — は、現在**クライアント側のドキュメント**に置かれて
います: 運用者の `CLAUDE.md`、リポジトリごとの指示、そして skill のテキストです。
ここには構造的な問題が 2 つあります:

1. **出所での分岐。** エージェントと環境の組み合わせ (Claude Code、ClotoCore の
   カーネルエージェント、claude.ai リモートコネクタ、将来のクライアント) は
   それぞれ自前のドクトリンの写しを持ち、その写しは食い違っていきます。新しい
   環境は、ドクトリンを*まったく*持たない状態で黙って始まります。
2. **運用者側の肥大。** ドクトリンは `CLAUDE.md` の場所を奪い合いますが、そこは
   既にスリム化の規律下にあります (セッションあたりのトークン固定費)。

サーバーは、定義上あらゆるクライアントが接続する唯一の構成要素です。そして MCP
には、そのために作られた配布チャネルがあります: `initialize` 応答の
`instructions` フィールドです。この設計は、CPersona に自身の運用コンテキストを
供給させます — **決定的な配布であり、確率的な遵守とは明示的に区別されます**。

これは「整形/制限は境界層の仕事」という原則 — 既にエージェントから見える
`limit` の上限に適用済み — を一般化したものです: *検証できる*ルールはサーバー側で強制し (Hard 層)、*述べることしかできない*
ルールはサーバー側から配布します (Soft 層)。

## 2. 2 層アーキテクチャ { #2-two-layer-architecture }

| 層 | 内容 | 機構 | 保証 |
| --- | --- | --- | --- |
| **Soft** | 挙動のドクトリン (recall の規律、agent_id の規約、セッションの習慣) | `initialize` → `instructions` の注入 + `get_operating_context` 詳細ツール | 決定的な**配布**。遵守は確率的なまま |
| **Hard** | 機械的に検査できるルール (有効な `project_id` の集合、`@auto` の既定解決) | ツール呼び出し時のサーバー側検証 | 決定的な**強制** (モードで制御) |

## 3. sidecar 設定ファイル (DB スキーマ変更なし) { #3-sidecar-configuration-file-no-db-schema }

先例は CScheduler の `~/.cscheduler/bindings.json` です。運用コンテキストは DB の
行ではなく**運用者が所有するファイル**です — これを第一選択の設計としたのは、
2.5.x の rollback-free 宣言をそのまま保てること、そしてガバナンスが自明になること
(§7) が理由です。

- **パス**: `~/.cpersona/operating-context.toml`。
  `CPERSONA_OPERATING_CONTEXT_PATH` で上書きできます。キルスイッチ:
  `CPERSONA_OPERATING_CONTEXT=off`。
- **形式**: TOML。標準ライブラリの `tomllib` で解析します (Python ≥3.11、新規
  依存はゼロ)。複数行のドクトリンテキストブロックのために、JSON ではなく TOML を
  選んでいます。
- **ファイルが無ければ機能は完全に休眠します。** instructions も検証もなく、挙動は
  一切変わりません。既存のデプロイには手が入りません。
- **ファイルが不正でも致命的にはなりません。** サーバーは機能を off にして起動し、
  警告をログに出し、`check_health` が finding を報告します (§8)。設定のタイプミス
  で記憶が止まるようなことがあってはなりません。
- **リロード**: 遅延・mtime ベースで、Hard 側のみです。`get_context()` はファイルの
  mtime が変わったときに再解析するので、レジストリ検証・`@auto` の解決・
  `get_operating_context` は、運用者の編集を次のツール呼び出しで反映します。
  `instructions` のテキストは**追随しません**: これはモジュールのインポート時に
  一度だけ読まれ
  (`registry = ToolRegistry(..., instructions=operating_context.instructions_text())`)、
  SDK の `Server` がその文字列を属性として保持し、`create_initialization_options()`
  は毎回それを返します — つまりプロセスの生存期間中は凍結されています。
  **再接続では足りません**: 稼働中のサーバーに対してクライアントが再度
  initialize しても、同じテキストが再び配られます。編集を配るにはサーバー
  プロセスの再起動が必要です。stdio クライアントはサーバーを起動し直すたびに
  プロセスも再起動するので、セッションをまたげば編集を拾います。1 つの長命
  プロセスがすべてのクライアントに応じる streamable-HTTP トランスポートでは
  拾いません。(本ドキュメントは以前、クライアントは再接続で更新を受け取ると
  記載していました — bug-252。)

### 3.1 スキーマ { #31-schema }

```toml
# ~/.cpersona/operating-context.toml
version = 1                       # file-format contract version
context_revision = "2026-07-16.1" # operator-owned label, echoed in all surfaces

[instructions]
# Compact canonical, injected verbatim via MCP initialize (§4). Keep small.
summary = """
CPersona operating context (rev 2026-07-16.1).
agent_id: 'claude-code' for Claude Code sessions, 'agent.<name>' for kernel agents.
project_id registry: "" (global), "acme-app". Pass "@auto" to resolve your default.
recall: limit<=5 outside session-start; use exclude_contents for known content.
Details: call get_operating_context.
"""

[registry]
project_ids = ["", "acme-app"]    # the valid project_id set
enforce = "warn"                  # "off" | "warn" | "reject"

[defaults]
# agent_id -> project_id; used ONLY to resolve the "@auto" sentinel (§5.2)
"claude-code" = ""

[[doctrine]]
name = "recall-discipline"
body = """
...full doctrine text served on demand by get_operating_context...
"""

[[doctrine]]
name = "agent-id-conventions"
body = """..."""
```

## 4. Soft 層: `initialize` の instructions { #4-soft-layer-initialize-instructions }

公式の MCP Python SDK は、既にこのフィールドを端から端まで運んでいます:
`Server(name, instructions=...)` → `create_initialization_options()` →
`InitializeResult.instructions` (vendored SDK の
`mcp/server/lowlevel/server.py:142,188` で確認済み)。cpersona 側の変更は、vendored
の `ToolRegistry.__init__` にそれを通すことだけです (現状は instructions なしの
`Server(server_name)`、`_vendored_mcp_common/mcp_utils.py:73`)。

組み立ての規則: `instructions = [instructions.summary]` をそのまま使い、前置きは
一切付けません。**summary が簡潔な正本であり、詳細は `get_operating_context` に
よる opt-in です** (プレビュー階層の構造で、CSC の `get_active_context` や recall
のプレビュー階層と同じトークン固定費の規律に従います)。

サイズの規律: summary は 1,500 文字以下に収めるべきです (SHOULD)。`check_health`
は 3,000 を超えると警告します (§8)。instructions のテキストは、接続している
すべてのクライアントにかかるセッションあたりの固定費です — doc ではなく
CLAUDE.md の予算として扱ってください。

### 4.1 クライアント伝播マトリクス (2026-07-16 実測) { #41-client-propagation-matrix-measured-2026-07-16 }

| クライアント | `instructions` の扱い | 状況 |
| --- | --- | --- |
| Claude Code (stdio + remote MCP) | "MCP Server Instructions" としてシステムコンテキストに注入する | **動作確認済み** (実セッションで、別サーバーの instructions が入るのをその場で観測) |
| ClotoCore kernel | **破棄される。** `initialize()` は `capabilities.mgp` と `capabilities.logging` だけを取り出し、結果はログに出したうえで捨てられる (`crates/core/src/managers/mcp_client.rs` の `initialize()`) | **欠落を確認** — カーネル側の作業項目であり、ClotoCore 側で追跡する (ここでは対象外)。それが入るまで、カーネルエージェントに届くのは Hard 層だけ |
| claude.ai リモートコネクタ | 未検証 | 実装中に**計測する**。コネクタが instructions を落とす場合でも、Claude Code のローカル stdio が主要環境をカバーする |

このカーネル側の欠落は 2.5.1 を止めません: Hard 層 (§5) はいずれにせよカーネル
エージェントに対して機械検査可能な部分集合を強制しますし、Soft 層はちょうど今日の
現状へと縮退するだけです。

## 5. Hard 層: レジストリ検証 + `@auto` センチネル { #5-hard-layer-registry-validation-auto-sentinel }

どちらの機構も、既存の project_id 意味論に対して**厳密に加算的**です。不変条件の
表:

| 呼び出し側が渡す値 | 挙動 (注記がなければ変更なし) |
| --- | --- |
| 省略 / `None` | 読み取り: フィルタなし。書き込み: `""` のグローバルプール。**変更なし。** |
| `""` | グローバルプール / グローバルのみのフィルタ。**変更なし、常に有効。** |
| 明示的な `"X"` | **新規**: `enforce` モードに従って `registry.project_ids` と照合されます。値そのものが書き換えられることは決してありません — 明示的な引数が上書きされることはありません |
| `"@auto"` | **新規**: opt-in のセンチネル。呼び出しの `agent_id` を使って `[defaults]` 経由で解決されます |

### 5.1 レジストリ検証 { #51-registry-validation }

`project_id` を受け取る 6 つのツールに適用されます (書き込み: `store`、
`archive_episode`。読み取り: `recall`、`recall_with_context`、`list_memories`、
`list_episodes` — `update_memory` は現行のツール契約では `project_id` を取り
ません)。モード:

- `off` — 検査しません (レジストリは文書としての意味しか持ちません)。
- `warn` (既定) — 未知の id を受理し、応答に
  `operating_context_warning: "project_id 'X' not in registry (rev ...)"` が
  付きます。advisory を先に置く方針であり、degraded-advisory と同じ哲学です:
  報告はする、壊さない。
- `reject` — **書き込み**で未知の id が来た場合、レジストリとリビジョンを名指し
  したエラーを返します。読み取りは依然として reject ではなく警告に留めます
  (誤った読み取りフィルタは何も失いませんが、誤った書き込みはバケットを汚します
  — この非対称性は実害の非対称性を写したものです)。

既定を `warn` にした理由: レジストリファイルは新しい仕組みであり、古くなった
レジストリが書き込みを壊してはなりません。柵が欲しい運用者は、意図的に `reject`
へ引き上げます。

### 5.2 `@auto` センチネル { #52-auto-sentinel }

- 解決: `defaults[agent_id]` → その project_id。対応が無ければ `""` に解決され、
  応答に `operating_context_warning` が付きます (`reject` モードでは代わりに
  エラー)。
- 解決された値は応答に `resolved_project_id` として返されます — 呼び出し側は実際に
  何が起きたのかを常に見られます (沈黙よりも透明性を取る)。
- `@auto` はリテラルであり opt-in です。これを送らない呼び出し側が影響を受ける
  ことはありません。明示的な値が書き換えられることはありません。そして解決された
  値は、明示的な値と同じようにレジストリ検証にかけられます。

## 6. ツール面: `get_operating_context` (24 → 25 ツール) { #6-tool-surface-get_operating_context-24-25-tools }

読み取り専用です。引数:

- (引数なし) → `{ context_revision, instructions_summary, registry: {project_ids, enforce},
  defaults, doctrine_sections: [names only], _meta }` — プレビュー階層です。
- `section: "recall-discipline"` → そのセクションの本文全文。

書き込みツールはありません (§7)。MCP のツール契約に対しては加算的です (新規ツール
のみで、既存ツールのシグネチャ変更はありません — §5 の応答フィールド追加は加算的な
フィールドであり、2.5.x のライン宣言はこれを契約維持として扱います。`persisted` /
`degraded` の先例と同じです)。

## 7. ガバナンス: 書き込み経路はファイルシステム { #7-governance-the-write-path-is-the-filesystem }

**2.5.1 には、運用コンテキストのための MCP 書き込みツールはありません。** sidecar
を編集するのは、OS を通した運用者だけです。以上。これは汚染経路 — 乗っ取られた、
あるいは混乱したエージェントがサーバーを説き伏せ、以後すべてのエージェントが
受け取るドクトリンを書き換えさせる経路 — に対して取りうる最も強いゲートです:

- MCP 面: 読み取り専用 (`get_operating_context`)。
- 書き込み面: ファイルのパーミッション — 運用者の所有であり、`CLAUDE.md` や
  `bindings.json` を編集するのと同じ信頼レベルです。
- キルスイッチ: `CPERSONA_OPERATING_CONTEXT=off` (環境変数、つまりこれも運用者の
  所有物)。

将来のバージョンがエージェント経由の編集 (例: 「この新しい project_id を登録して」)
を求めるなら、それは明示的な承認機構を伴う別の設計として入ります — ここでは意図的
に対象外としています。

## 8. ヘルスチェックとの統合 { #8-health-integration }

`check_health` のレジストリ (v2.4.37 のレジストリアーキテクチャ) に、加算的な
チェックを 2 つ追加します:

- `operating_context_parse` — sidecar は存在するが解析できない / スキーマ不正
  (severity: warn、`fix=false` — 修復とは人がファイルを編集することです)。
- `operating_context_size` — instructions の summary が 3,000 文字超
  (severity: info、固定費の規律)。

## 9. テスト計画 { #9-testing-plan }

密閉環境で行い (tmp ディレクトリの sidecar + 環境変数による上書き)、稼働中の
バックエンドは不要です:

1. sidecar が無い / `off` / 不正 → 機能は休眠し、挙動の差分はゼロ (既存スイート
   全体に対する回帰ガード)。
2. instructions の引き回し: `ToolRegistry(instructions=...)` が
   `create_initialization_options()` に届くこと。
3. レジストリのモード: off/warn/reject × 読み取り/書き込み × 既知/未知/`""` の
   project_id。
4. `@auto`: 対応あり、対応なし、明示的な値が決して書き換えられないこと、
   `resolved_project_id` が返ること。
5. mtime によるリロード: セッション途中で sidecar を編集 → 次の呼び出しが新しい
   レジストリを見ること。Hard 層のみです — 同じ編集をしても、再接続したクライアント
   に配られる `instructions` は変わらず、プロセスが再起動するまで凍結されたまま
   であり (§3)、テストがこの非対称性を固定します。
6. §8 の 2 つの条件でヘルスチェックが発火すること。

## 10. バージョン上の位置づけとリリース経路 { #10-version-position-release-path }

- **2.5.1 として直接リリース** (オーナー裁定 2026-07-16): 加算的な機能であり、
  スキーマ / 契約の破壊がないため、pre-release ladder は踏みません。2.5.0 の a/b
  ladder は*破壊を伴う*内部安定化に対する用心であって、一般則ではありませんでした。
  RELEASE_LIFECYCLE_STANDARD v1.2 (ライン内のフィーチャーサイクル + ladder の発動
  基準) を必要とします — 同じバッチで出す付随改訂です。
- タグ付け / リリースは **2.5.0 final の後**に限ります。設計とブランチ実装は b1 の
  soak と並行して進めます。注意: soak 中に 2.5.1 を出すと、2.5.x の Stable 認定の
  時計は事実上リセットされます (認定はその後、2.5.1 を含めた形で取ることに
  なります)。

## 11. 未解決の論点 { #11-open-questions }

1. claude.ai リモートコネクタでの伝播 (§4.1) — 計測すること。結果が設計を変える
   ことはなく、カバレッジの主張だけが変わります。
2. ClotoCore カーネルの `instructions` 対応 — ClotoCore 側の issue / goal として
   起票すること。エージェントのシステムプロンプトへ全体に注入するのか、エージェント
   ごとに注入するのかを決めること。
3. `[defaults]` はワイルドカードキー (`"*"`) を持つべきか? 具体的な必要が出るまで
   保留 — YAGNI ですし、ワイルドカードは `@auto` の明示性を弱めます。
