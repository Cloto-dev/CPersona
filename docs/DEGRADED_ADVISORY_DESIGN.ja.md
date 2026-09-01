<!-- i18n-source: docs/DEGRADED_ADVISORY_DESIGN.md@blob:236dc2b8fc3356d1308168162229bc5ca239309f -->

# dense 劣化のランタイム検知 + advisory のコンテキスト注入

**ステータス**: 設計 (Route B を 2.4.x ラインで採用。Route A は 2.5.0 で予定)
**決定**: プロジェクトオーナー + claude-code、2026-06-28 (引き継ぎ: CPersona memory `id 1165`, `agent_id=claude-web`, `project_id=cloto`)
**範囲**: 外科的パッチ — SCHEMA 変更なし、新規ツールなし。応答フィールド 1 つ + プロセスレベルの health 状態 + 環境変数 1 つ。

> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

---

## 1. 動機 { #1-motivation }

同梱スキルは**セットアップ時**のセルフチェックを実行します。これは
スナップショットです: *インストール時点*で埋め込みバックエンドに到達できたことを
証明するだけです。**その後に劣化状態へずれ込む**埋め込みは捕まえられません —
プロセスが死ぬ、DB が別マシンにコピーされる、ポートが変わる、起動時の競合で
`mode=http` が何も指していない状態になる、といったケースです。

いずれの場合も CPersona は `recall` に応答し続け、静かに FTS のみへ劣化します。
「動いてはいるが劣化している」は**評判上の負債**です。とりわけ SKILL が応えようと
している気軽な / vibe-coder 層 (「CPersona をとりあえず作って」) に対してはそうです。
本設計は **SKILL のインストールゲートと対を成すランタイムガード**であり、静かな失敗を
自己申告する失敗へと反転させます。

この問題はコード中で既に認識されています:

```python
# config.py:14
# silently off (recall degraded to FTS-only) — bug-001.
EMBEDDING_MODE = os.environ.get("CPERSONA_EMBEDDING_MODE") or os.environ.get("EMBEDDING_MODE", "none")
```

`bug-001` は env キーの修正 (静的な、インストール時の半分) でした。本設計はその
**ランタイム側の後継**です: 同じ罠のリストを、インストールゲート (SKILL) と
ランタイムガード (ここ) で共有します。

---

## 2. 現行コード: 劣化はどこで握り潰されているか { #2-current-code-where-degraded-is-swallowed }

調査対象は `master` (`v2.4.32`, `48e2cef`)。

### 2.1 中核の握り潰し — `EmbeddingClient.embed()` { #21-the-core-swallow-embeddingclientembed }

`_vendored_mcp_common/embedding_client.py:102-135`:

```python
async def embed(self, texts):
    if self.mode == "none" or not self._client:
        return None                              # (a) FTS-only by configuration
    ...
    try:
        if self.mode == "http":
            result = await self._embed_via_http(texts)
        ...
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError) as e:
        logger.warning(...)
        return None                              # (b) http reachable-but-down, swallowed
```

**(a) と (b) はどちらも `None` に潰れます。** 呼び出し側は「埋め込みは意図的に
off」と「埋め込みは設定されているがエンドポイントが死んでいる」を区別できません。
この 2 つを判別することが本機能の核心です。

### 2.2 二次的な握り潰し — リモートベクトル検索 { #22-the-secondary-swallow-remote-vector-search }

`vector.py:205`:

```python
except Exception as e:
    logger.warning("Remote vector search failed, falling back to local: %s", e)
```

形は同じです: 実際の障害はログに残るだけで、静かに格下げされます。

### 2.3 advisory の着地点 — `do_recall` { #23-the-advisory-landing-site-do_recall }

`memory_handlers.py:702` の `do_recall(...)` は `:825` で単一の構造を返します:

```python
return {"messages": messages}
```

advisory はここに兄弟フィールドとして付きます。`test_do_recall_response.py` は
この応答契約を既に回帰テストしているので、拡張できるテストカバレッジが既にあります。

### 2.4 状態保持の前例 { #24-the-state-storage-precedent }

**プロセスレベルのモジュール状態**は本コードベースに既に存在します: no-persist の
トグルと、`vector.py` の agent ごとの `dict` (`_agent_thresholds`,
`_agent_fused_gates`) です。health 状態も同じ置き方をします — モジュール
シングルトンで、再起動時にリセットされます。

---

## 3. 確定仕様 (9 項目、引き継ぎ `id 1165` より) { #3-confirmed-spec-9-points-from-handoff-id-1165 }

| # | 仕様 | コード上の着地点 |
|---|------|-----------------|
| 1 | 設定の読み取りではなく**実測**による検知。埋め込みクライアント境界で `{attempted, ok, error}` を表出し、`do_recall` がそれを読む。 | `_search_vector` 内の `embed()` 呼び出し地点に `health.observe_*` 呼び出しを新設。`do_recall` は `health.snapshot()` を読む。 |
| 2 | **状態機械、4 状態**: `unknown` / `healthy` / `hint` / `fault`。プロセスレベル (再起動時にリセット)。 | `health.py` のモジュールシングルトンを新設 (no-persist のモジュール状態に倣う)。 |
| 3 | **深刻度の分割**: `hint` = 埋め込み未設定 (`mode=none`、FTS のみ、静的 → 即時)。`fault` = `mode=http` だがエンドポイントに到達不能 (**連続 2 回**の失敗で昇格。単発のブレは CoreML ハングの前例に倣ってデバウンスする)。 | `hint` は `config.EMBEDDING_MODE` から設定。`fault` は連続失敗カウンタでゲートする。 |
| 4 | **遷移で発火**: `healthy→degraded` の最初の遷移ごとに **~1000 文字のフル**テンプレートを 1 回だけ出す。*同一*障害中の以降の recall では **~100 文字のショート**リマインダを出す。`healthy` は**完全に沈黙**する。 | `health` が `advisory_emitted_for_current_outage` を記録し、`do_recall` がフル / ショート / なしを選ぶ。 |
| 5 | **動的な証跡**をフルとショートの両方のペイロードに埋め込む (例: `mode=http / POST http://127.0.0.1:8401/embed failed: connection refused`)。テンプレート = 静的な骨組み、問題 = 動的なスロット。 | `health.evidence` はプローブ (Route B) が埋める — 実際に捕捉したエラー文字列。 |
| 6 | **ペイロード = 構造体** `{degraded, severity, reason, evidence, runbook}`。レンダリングとローカライズはエージェントが行う (言語と口調はエージェントの領分)。命令形の言い回し (「ユーザーに通知せよ: ...」) は伝達される確率を上げる。 | `advisory` フィールドの値がこの構造体。レンダリングはクライアントに委ねる。 |
| 7 | **搬送路 = `recall` 応答の advisory フィールド**。MCP は push できない → 正直に言える到達範囲は「fault は*次の* recall で表面化する」。伝達はベストエフォートであり、そう明記しなければならない。 | `messages` と並ぶ `advisory` キーを新設。 |
| 8 | **既定で on / 環境変数で opt-out。** opt-out は、埋め込みバックエンド無しでの運用を運用者が受け入れたことを記録します — サポートされる fallback であって推奨ではありません。安全側が既定。 | `CPERSONA_DEGRADED_ADVISORY` (既定 `true`)。 |
| 9 | **`fault` の runbook 骨組み**: 状態 + 実測した証跡 / 影響 (平易な言葉で) / 調査手順 / 修復コマンド / ユーザー向けの平易な 1 文 / opt-out の環境変数。 | `health.py` 内の静的テンプレート文字列。 |

---

## 4. Route B (採用、2.4.x ライン) — cpersona ローカルのプローブ { #4-route-b-accepted-24x-line-cpersona-local-probe }

### 4.1 なぜ今 Route B なのか { #41-why-route-b-now }

`embed()` は `_vendored_mcp_common/` にあります — 共有 common を CPersona へ vendor
したものです。`embed()` 自身に `{attempted, ok, error}` を表出させる (Route A) には
clotohub-servers の common bump + 再 vendor が必要で、他のすべての consumer に
波及します。これは引き継ぎの「外科的パッチ / 新規ツールなし / 2.4.x QOL ライン」
という枠組みに反します。

Route B は変更を **cpersona だけ**に留めます: `embed()` には手を触れず、CPersona は
health を (a) 静的な `hint` のケースについては `config.EMBEDDING_MODE` から、
(b) `fault` のケースについては**自前の軽量なヘルスプローブ**から導出し、プローブ
自身の `try/except` が見た実際のエラー文字列を捕捉します。

### 4.2 新規モジュール — `health.py` { #42-new-module-healthpy }

```python
"""Process-level embedding-health state for the degraded-advisory guard.

Module singleton, reset on restart (mirrors the no-persist module-state). Fed by
observations from the recall path; read by do_recall to attach an advisory.
"""

# 4 states (point 2)
UNKNOWN, HEALTHY, HINT, FAULT = "unknown", "healthy", "hint", "fault"

_state = UNKNOWN
_severity = None            # "hint" | "fault"
_reason = None              # short machine reason
_evidence = None           # dynamic: the measured failure, e.g. "POST .../embed: connection refused"
_consecutive_failures = 0   # debounce counter (point 3)
_advisory_emitted = False   # full-vs-short selector (point 4)

FAULT_PROMOTE_THRESHOLD = 2  # consecutive failures before healthy->fault (point 3)
```

主要な遷移:

- **`observe_config()`** (do_recall の入口で 1 回呼ぶ): `EMBEDDING_MODE == "none"`
  なら即座に `HINT` を設定 (静的、デバウンスなし)。そうでなければ http 経路は
  プローブに任せる。
- **`observe_ok()`**: 埋め込みが使えるベクトルを返した → `HEALTHY` にし、
  `_consecutive_failures` をリセット、`_advisory_emitted` をクリア (後で再び失敗した
  ときにフルテンプレートを再送出するため — 項目 4 の「復旧→再失敗で再武装」)。
- **`observe_failure(evidence)`**: `mode=http` の試行が失敗 → `_consecutive_failures += 1`。
  `>= FAULT_PROMOTE_THRESHOLD` になったときにのみ `FAULT` へ昇格 (単発のブレを
  デバウンス)。

### 4.3 プローブ { #43-the-probe }

`_search_vector` が `embed([query])` を呼び (`vector.py:182`)、
`EMBEDDING_MODE != "none"` の状態で falsy な結果を得たとき、CPersona は
`_probe_embedding_health()` を実行します:

```python
async def _probe_embedding_health() -> tuple[bool, str | None]:
    """Direct, non-swallowing health POST to the embedding endpoint.

    Returns (ok, error_string). Unlike embed(), this does NOT swallow — it captures
    the actual transport error for the advisory's evidence slot (point 5).
    """
    client = vector._embedding_client
    try:
        resp = await client._client.post(client._http_url, json={...minimal probe...}, timeout=...)
        resp.raise_for_status()
        return True, None
    except Exception as e:
        return False, f"mode=http / POST {client._http_url} failed: {e}"
```

- プローブは**失敗が疑われるときだけ**走ります (非空のクエリに対して embed が falsy を
  返した場合)。recall のたびに走るわけではありません — 追加 I/O は有界で、埋め込み
  キャッシュが繰り返しを吸収します。
- プローブが捕捉したエラーが**動的な証跡** (項目 5) です。
- デバウンス (項目 3): プローブの連続 2 回の失敗で `HINT`/`HEALTHY`→`FAULT` へ
  昇格します。

> **二重 I/O / 競合についての注記**: Route B のプローブは、実際の recall 経路の
> `embed()` 呼び出しとは*別の* POST です。そのため原理的にはプローブと実呼び出しが
> 食い違い得ます (片方が成功し、もう片方が失敗する)。ベストエフォートの advisory と
> しては許容範囲であり、まさにこの継ぎ目を Route A が 2.5.0 で取り除きます (§6)。

### 4.4 `do_recall` への統合 { #44-do_recall-integration }

`do_recall` の入口で `health.observe_config()` を呼びます。recall 経路はプローブ
経由で `observe_ok()` / `observe_failure()` に供給します。
`return {"messages": messages}` の直前で:

```python
advisory = health.maybe_advisory()  # None when healthy/opted-out; full or short struct otherwise
if advisory is not None:
    return {"messages": messages, "advisory": advisory}
return {"messages": messages}
```

`maybe_advisory()` は `_state == HEALTHY` のとき、または環境変数で opt-out されて
いるときに `None` を返します。障害の最初の遷移では**フル**の構造体を返し
(`not _advisory_emitted` のとき。返した後にフラグを立てる)、同一障害中の以降の
recall では**ショート**の構造体を返します。

### 4.5 advisory のペイロード (項目 6) { #45-advisory-payload-point-6 }

```jsonc
{
  "degraded": true,
  "severity": "fault",                      // or "hint"
  "reason": "embedding endpoint unreachable",
  "evidence": "mode=http / POST http://127.0.0.1:8401/embed failed: connection refused",
  "runbook": "<full or short text per point 4/9>"
}
```

`fault` の `runbook` (フル、項目 9 の骨組み): 状態 + 実測した証跡 / 平易な言葉での
影響 / 調査手順 (プロセスは生きているか? ポートは? `curl` の結果は? モデルは
ダウンロード済みか?) / 修復コマンド (埋め込みサーバーを起動する / URL とポートを
直す / バックエンドを入れ直すならセットアップ手順をやり直す) / ユーザー向けの
平易な 1 文 / opt-out の環境変数。
伝達される確率を上げるため命令形で書きます (項目 6)。

### 4.6 環境変数による opt-out (項目 8) { #46-env-opt-out-point-8 }

```python
DEGRADED_ADVISORY_ENABLED = os.environ.get("CPERSONA_DEGRADED_ADVISORY", "true").lower() == "true"
```

既定で on。opt-out は、埋め込みバックエンド無しでの運用を受け入れた運用者に対して
advisory を黙らせます。その構成が推奨になるわけではありません。

### 4.7 テスト { #47-tests }

- `test_do_recall_response.py` を拡張する: (a) `mode=none` → `hint` の advisory が
  付く。(b) `mode=http` + プローブが 2 回失敗 → 証跡付きの `fault` advisory。
  (c) 単発のブレ (1 回の失敗) → advisory は**出ない** (デバウンス)。(d) `healthy` →
  `advisory` キー自体が付かない。(e) 1 つの障害中の 2 回の recall でフル→ショート。
  (f) 復旧で状態がクリアされ再武装する。(g) 環境変数の opt-out ですべてが沈黙する。
  プローブは monkeypatch する (稼働中のエンドポイントは不要)。

---

## 5. 対象外 { #5-out-of-scope }

- SCHEMA 変更なし、新規 MCP ツールなし (応答フィールド + 環境変数のみ)。
- push はしない (MCP にはできない) — 到達範囲は「次の recall で表面化する」(項目 7)
  であり、正直にそう述べる。
- bge-m3 の mac CoreML ハング対策はベストエフォート / 未検証のまま (引き継ぎの
  未解決項目)。

---

## 6. Route A — CPersona 2.5.0 で予定 { #6-route-a-planned-for-cpersona-250 }

メジャーバージョンによってリポジトリをまたぐ common bump が許容できるようになった
時点で、検知を境界そのものへ畳み込みます: `EmbeddingClient.embed()` が `None` へ
潰れる代わりに、ネイティブに `{attempted, ok, error}` を返す (あるいは型付きエラーを
送出する) ようにします。そうすれば CPersona ローカルのプローブ (§4.3) は**削除**され、
health 状態は実際の recall 経路の `embed()` の結果から直接供給されます。

**なぜこの層分けが綺麗なのか (前方互換性)**: Route B の **advisory 契約が安定した
インターフェース**です — ペイロード構造体
`{degraded, severity, reason, evidence, runbook}` と `do_recall` の `advisory`
フィールドは変わりません。Route A は**「シグナル源の差し替え」**のリファクタリング
(プローブ → embed() の結果) であって、再設計ではありません。利用者から見える契約は
同一で、証跡は別建てのプローブ POST から実際の recall 経路の呼び出しへと*格上げ*され、
§4.3 の二重 I/O と、プローブ対実呼び出しの競合が解消されます。

2.5.0 で見積もるべきリポジトリ横断のコスト: clotohub-servers の `servers/common/`
変更 → `clotohub-servers-common` の bump → CPersona への再 vendor → 他の consumer の
再検証 (CScheduler の埋め込み等)。

---

## 7. 実装メモ / 訂正 (v2.4.33 ビルド) { #7-implementation-notes-corrections-v2433-build }

Route B の実装中に判明した精緻化です。前の各節と衝突する箇所では、こちらが優先されます。

1. **`HINT→FAULT` の経路は存在しない** (§4.3 の記述を上書き)。`EMBEDDING_MODE=="none"`
   のとき `server.py:959` はクライアントを構築しないため、`vector._embedding_client is None`
   となり、embed / プローブの経路には決して入りません。したがって `hint` は `do_recall`
   入口の `health.observe_config()` *だけ*で検知され、`fault` は常に `unknown`/`healthy`
   からのみ昇格します。
2. **advisory の返却地点は 2 つある** (§2.3 の「`:825` の単一の構造」という枠組みを
   上書き)。`do_recall_with_context` は自前の返却値を組み立て、`do_recall` の結果からは
   `messages` しか取り出しません。そのため `recall_result.get("advisory")` を明示的に
   **転送**しなければ advisory は落ちます。`maybe_advisory()` を再度呼んではなりません
   (MUST NOT) — 1 回の論理的な recall の中でフル→ショートに切り替わってしまうためです。
3. **プローブの配置** (§4.3 の詳細化)。`_probe_embedding_health()` は `vector.py` に
   置きます (`_embedding_client` と `httpx` が必要であり、また `health.py` に `vector` の
   import を持ち込まないことで依存グラフを `config ← health ← vector ← memory_handlers`
   に保つため)。専用の短い `PROBE_TIMEOUT_SECS=3.0` を使い (embed の 30 秒タイムアウトでは
   ありません)、`health.is_faulted()` でゲートされるため、プローブの I/O は 2 回の
   プローブによる昇格ウィンドウに限定されます。復旧は embed の**成功**経路で観測され、
   再プローブによってではありません。
4. **リモート検索の握り潰しに別途フックは不要** (§2.2 の詳細化)。
   `VECTOR_SEARCH_MODE=="remote"` の場合、リモートの失敗は計測を仕込んだローカルの embed
   経路へフォールスルーします。そのため配線するのはローカル経路だけです (本番はローカル
   モードを使用)。
5. **ツールスキーマの変更なし** — `_vendored_mcp_common/mcp_utils.py` がハンドラの返却
   dict 全体を `json.dumps` するため、追加した `advisory` キーは何もしなくても
   クライアントに届きます。

**ファイル**: `health.py` (新規)、`vector.py` (ローカルの embed 経路でのプローブ +
observe)、`memory_handlers.py` (入口の `observe_config`、両方の返却地点での advisory)、
`config.py` (`CPERSONA_DEGRADED_ADVISORY`)、`test_do_recall_response.py` (状態機械の
単体テスト + do_recall の統合テスト + プローブの単体テスト。autouse の `health._reset`)。
テスト: 13/13 green。recall-SQL の回帰 `test_channel_axis_migration` 7/7 +
`test_episode_channel` 10/10 green。

---

## 8. 抑制のスコープ (bug-251) { #8-suppression-scope-bug-251 }

§3 の項目 4 の発火ルール、および §4.5 / §6 のペイロードのフィールド一覧を supersede します。

**欠陥。** 「完全版 runbook はもう発火した」はプロセスの状態 (`health._advisory_emitted`)
であり、エピソードごと 1 回の縮退はこれを鍵にしています。stdio ではこれが意図どおりの
ルールです — 1 プロセスが 1 クライアントセッションを相手にするからです。しかし
`CPERSONA_TRANSPORT=streamable-http` は `StreamableHTTPSessionManager(stateless=True)` で
動くため、1 プロセスが接続中の全クライアントに応答します。障害中の最初の recall が
全員分の完全版 runbook を消費し、他のすべてのセッションには `FAULT_RUNBOOK_SHORT` が
渡っていました。これは `**Notify the user:**` の命令を持たず、そのセッションが受け取って
いないメッセージへの続報として読めてしまいます。項目 7 の正直な到達範囲 —
「fault は*次の* recall で表面化する」— は、いつのまにか「あるひとつのセッションの次の
recall で、障害ごとに 1 回だけ」になっていました。

実トランスポート経由で計測: 1 プロセス・1 回の障害で 2 クライアントが recall したところ、
最初は命令付きの 1067 文字、2 番目は命令なしの 107 文字でした。

**現在のルール。** プロセスが複数セッションを相手にしている間、`fault` は縮退しません。
障害は稀であり runbook はこの機能の目的そのものなので、recall ごとにその費用を払う方が、
1 つを除く全セッションで沈黙の費用を払うより良い、という判断です。

`hint` は従来どおり縮退します。`mode=none` は恒久的な状態なので、免除するとフル版の
runbook を recall ごとに永久に繰り返すことになります。埋め込みバックエンド無しでの運用は
障害ではなく継続的な状態であり、都度エスカレーションすべきものではありません。

**どちらのルールが効いているかはペイロードが述べます。** `advisory_scope` は抑制状態が
共有されている時に `"process"`、プロセスがそのままセッションである時に `"session"` を
返します。これによりクライアントは、受け取っていないものへの続報と、受け取ったものへの
続報を区別できます。no-persist のトグルも同じやり方で影響範囲を開示しています。この
advisory はそうせず、自分のペイロードを黙って劣化させていました。

**なぜセッション単位にしないのか。** recall の継ぎ目にはセッションを識別できるものが
ありません。HTTP モードは stateless なのでリクエストを越えて残るセッションはなく、ACL の
principal はクライアント id しか持ちません — 1 つの資格情報を共有する 2 つのウィンドウは
1 つの principal です。呼び出し側が渡す鍵があればセッション単位にできますが、その代償は
全クライアントが渡さねばならない引数です。`advisory_scope` は、その時に `"session"` を
返し始めるフィールドであり、形は変わりません。
