<!-- i18n-source: docs/DEGRADED_ADVISORY_DESIGN.md@blob:43443e625dd2c7ee573f2b0ef1b76ea2afb01160 -->

# 埋め込み劣化: ランタイム検知 + advisory 注入

**ステータス**: 実装済。Route B は 2.4.x ラインで出荷し、Route A がそれを置き換えました。
証跡は失敗した埋め込み呼び出し自身から得られ、プローブは撤去されています (§6)。
**決定**: プロジェクトオーナー + claude-code、2026-06-28 (引き継ぎ: CPersona memory `id 1165`, `agent_id=claude-web`, `project_id=cloto`)
**範囲**: 外科的パッチ。SCHEMA 変更なし、新規ツールなし。応答フィールド 1 つ、プロセスレベルの health 状態 1 つ、環境変数 1 つ。

> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

---

## 1. 動機 { #1-motivation }

同梱スキルは**セットアップ時**のセルフチェックを実行します。これはスナップショット
です。*インストール時点*で埋め込みバックエンドに到達できたことを証明するだけです。

**その後に劣化状態へずれ込む**埋め込みは捕まえられません。プロセスが死ぬ、DB が別マシンに
コピーされる、ポートが変わる、起動時の競合で `mode=http` が何も指していない状態になる、と
いったケースです。いずれの場合も CPersona は `recall` に応答し続け、FTS のみへ劣化し、
そのことをどこにも告げません。

「動いてはいるが劣化している」は**評判上の負債**です。とりわけ SKILL が応えようと
している気軽な / vibe-coder 層 (「CPersona をとりあえず作って」) に対してはそうです。
本設計は **SKILL のインストールゲートと対を成すランタイムガード**であり、告げられない
失敗を、自己申告する失敗へと反転させます。

この問題は、コード中で既に認識されています。

```python
# config.py:14
# silently off (recall degraded to FTS-only) — bug-001.
EMBEDDING_MODE = os.environ.get("CPERSONA_EMBEDDING_MODE") or os.environ.get("EMBEDDING_MODE", "none")
```

`bug-001` は env キーの修正で、静的な、インストール時の半分でした。本設計はその
**ランタイム側の後継**です。同じ罠のリストを、インストールゲート (SKILL) とランタイム
ガード (ここ) で共有します。

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

**(a) と (b) はどちらも `None` に潰れます。** 呼び出し側は「埋め込みは意図的に off」と
「埋め込みは設定されているがエンドポイントが死んでいる」を区別できません。この 2 つを
判別することが、本機能の核心です。

### 2.2 二次的な握り潰し — リモートベクトル検索 { #22-the-secondary-swallow-remote-vector-search }

`vector.py:205`:

```python
except Exception as e:
    logger.warning("Remote vector search failed, falling back to local: %s", e)
```

形は同じです。実際の障害はログに残るだけで、呼び出し側には何も伝わらないまま格下げされます。

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
| 5 | **動的な証跡**をフルとショートの両方のペイロードに埋め込む (例: `mode=http / POST http://127.0.0.1:8401/embed failed: connection refused`)。テンプレート = 静的な骨組み、問題 = 動的なスロット。 | `health.evidence` は埋め込み呼び出し自身が捕捉したエラーを運ぶ。 |
| 6 | **ペイロード = 構造体** `{degraded, severity, reason, evidence, runbook}`。レンダリングとローカライズはエージェントが行う (言語と口調はエージェントの領分)。命令形の言い回し (「ユーザーに通知せよ: ...」) は伝達される確率を上げる。 | `advisory` フィールドの値がこの構造体。レンダリングはクライアントに委ねる。 |
| 7 | **搬送路 = `recall` 応答の advisory フィールド**。MCP は push できない → 正直に言える到達範囲は「fault は*次の* recall で表面化する」。伝達はベストエフォートであり、そう明記しなければならない。 | `messages` と並ぶ `advisory` キーを新設。 |
| 8 | **既定で on / 環境変数で opt-out。** opt-out は、埋め込みバックエンド無しでの運用を運用者が受け入れたことを記録します — サポートされる fallback であって推奨ではありません。安全側が既定。 | `CPERSONA_DEGRADED_ADVISORY` (既定 `true`)。 |
| 9 | **`fault` の runbook 骨組み**: 状態 + 実測した証跡 / 影響 (平易な言葉で) / 調査手順 / 修復コマンド / ユーザー向けの平易な 1 文 / opt-out の環境変数。 | `health.py` 内の静的テンプレート文字列。 |

---

## 4. Route B (採用、2.4.x ライン) — cpersona ローカルのプローブ { #4-route-b-accepted-24x-line-cpersona-local-probe }

### 4.1 なぜ今 Route B なのか { #41-why-route-b-now }

`embed()` は `_vendored_mcp_common/` にあります。共有クライアントを CPersona へ vendor
したものです。当時の判断では、`embed()` 自身に `{attempted, ok, error}` を表出させるには
上流パッケージのリリースと、全 consumer が吸収すべき変更が要るとされ、「外科的パッチ /
新規ツールなし / 2.4.x QOL ライン」という枠組みに反するとされました。

この前提は、実際の変更 (§6) を通りませんでした。additive なメソッドは既存の入口を一切
変えないため、どの consumer も何も吸収する必要がありませんでした。

この前提は実際の変更 (§6) を通りませんでした: additive なメソッドは既存の入口を一切
変えないため、どの consumer も何も吸収する必要がありませんでした。

Route B は変更を **cpersona だけ**に留めます。`embed()` には手を触れず、CPersona は
health を 2 つの源から導出します。静的な `hint` のケースは `config.EMBEDDING_MODE` から、
`fault` のケースは**自前の軽量なヘルスプローブ**からで、後者はプローブ自身の
`try/except` が見た実際のエラー文字列を捕捉します。

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

主要な遷移は次のとおりです。

- **`observe_config()`** (do_recall の入口で 1 回呼ぶ)。`EMBEDDING_MODE == "none"`
  なら即座に `HINT` を設定します (静的、デバウンスなし)。そうでなければ http 経路は
  プローブに任せます。
- **`observe_ok()`**: 埋め込みが使えるベクトルを返したので `HEALTHY` にし、
  `_consecutive_failures` をリセットし、`_advisory_emitted` をクリアします (後で再び
  失敗したときにフルテンプレートを再送出するため。項目 4 の「復旧 → 再失敗で再武装」)。
- **`observe_failure(evidence)`**: `mode=http` の試行が失敗したので
  `_consecutive_failures += 1`。`>= FAULT_PROMOTE_THRESHOLD` になったときにのみ `FAULT`
  へ昇格します (単発のブレをデバウンス)。

### 4.3 プローブ { #43-the-probe }

> **§6 により置き換え済。** ここで説明するプローブはもう存在しません。これが必要だった
> 理由が、現在の設計がこの形をしている理由なので残してあります。

`_search_vector` が `embed([query])` を呼び、`EMBEDDING_MODE != "none"` の状態で
falsy な結果を得たとき、CPersona は `_probe_embedding_health()` を実行していました。

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
  返した場合)。recall のたびに走るわけではありません。追加 I/O は有界で、埋め込み
  キャッシュが繰り返しを吸収します。
- プローブが捕捉したエラーが、**動的な証跡** (項目 5) です。
- デバウンス (項目 3): プローブの連続 2 回の失敗で、`HINT` / `HEALTHY` から `FAULT` へ
  昇格します。

> **二重 I/O と、それが許していた食い違い。** プローブは実際の recall 経路の `embed()`
> 呼び出しとは*別の* POST でした。そのため両者は食い違い得ます。そして問題になる向きは
> 「プローブが成功し、実呼び出しが失敗する」方でした。この分岐は、何も返さなかった
> recall に対して health を OK として記録してしまうからです。
>
> さらにプローブは、ローカルサーバー用のペイロード形状を `_http_url` に POST します。
> それを持たない api モードでは、証跡ではなく "embedding client unavailable" しか
> 出せませんでした。どちらもプローブと共に消えています (§6)。

### 4.4 `do_recall` への統合 { #44-do_recall-integration }

`do_recall` の入口で `health.observe_config()` を呼びます。recall 経路は、埋め込み
呼び出し自身の結果から `observe_ok()` / `observe_failure()` に供給します。

ただし `observe_ok()` はその結果が `attempted` を報告した時だけです。クライアントは
同一の単一テキスト埋め込みを TTL キャッシュから返し、その際プロセスの外へ要求は出ません。
出ていない呼び出しの値は、バックエンドの観測ではないからです (bug-248)。キャッシュヒットは、
最後の実際の呼び出しが置いた状態をそのまま残します。

`return {"messages": messages}` の直前で、次を行います。

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

`fault` の `runbook` は、フル版 (項目 9 の骨組み) では次を運びます。状態と実測した証跡、
平易な言葉での影響、調査手順 (プロセスは生きているか? ポートは? `curl` の結果は?
モデルはダウンロード済みか?)、修復コマンド (埋め込みサーバーを起動する、URL とポートを
直す、バックエンドを入れ直すならセットアップ手順をやり直す)、ユーザー向けの平易な 1 文、
そして opt-out の環境変数です。伝達される確率を上げるため、命令形で書きます (項目 6)。

### 4.6 環境変数による opt-out (項目 8) { #46-env-opt-out-point-8 }

```python
DEGRADED_ADVISORY_ENABLED = os.environ.get("CPERSONA_DEGRADED_ADVISORY", "true").lower() == "true"
```

既定で on です。opt-out は、埋め込みバックエンド無しでの運用を受け入れた運用者に対して
advisory を黙らせます。その構成が推奨になるわけではありません。

### 4.7 テスト { #47-tests }

`test_do_recall_response.py` を次のように拡張します。(a) `mode=none` で `hint` の
advisory が付く。(b) `mode=http` でプローブが 2 回失敗し、証跡付きの `fault` advisory が
出る。(c) 単発のブレ (1 回の失敗) では advisory が**出ない** (デバウンス)。(d) `healthy`
では `advisory` キー自体が付かない。(e) 1 つの障害中の 2 回の recall でフルからショートへ
変わる。(f) 復旧で状態がクリアされ、再武装する。(g) 環境変数の opt-out ですべてが沈黙する。
プローブは monkeypatch するので、稼働中のエンドポイントは不要です。

---

## 5. 対象外 { #5-out-of-scope }

- SCHEMA 変更なし、新規 MCP ツールなし。応答フィールドと環境変数のみです。
- push はしません (MCP にはできません)。到達範囲は「次の recall で表面化する」(項目 7)
  であり、正直にそう述べます。
- bge-m3 の mac CoreML ハング対策は、ベストエフォート / 未検証のままです (引き継ぎの
  未解決項目)。

---

## 6. Route A — 出荷済 { #6-route-a-shipped }

検知は境界そのものへ畳み込まれ、プローブ (§4.3) は**削除**されました。health 状態は、
実際の recall 経路の呼び出しから直接供給されます。

§4.1 が想定した破壊的変更は不要でした。`embed()` はシグネチャも戻り値もそのままで、
additive な `embed_with_outcome()` が同じ値と併せて `{attempted, ok, error}` を返し、
recall 経路がそちらを呼びます。他に変える必要が無かったため、メジャーバージョンを待たず
2.5.x ラインで着地しています。

outcome は client に保持せず呼び出し元へ返すので、並行する 2 つの embed が互いの結果を
読むことはありません。1 つ挙げておく価値があるのは、**埋め込みを含まない 2xx 応答を失敗
として報告する**点です。呼び出し元にとってそれは失敗であり、しかも別建てのプローブが
まさに取り違えていたケース (同じ成功コードを見てしまう) だからです。

**なぜこの層分けが綺麗なのか (前方互換性)。** Route B の **advisory 契約が安定した
インターフェース**です。ペイロード構造体
`{degraded, severity, reason, evidence, runbook}` と `do_recall` の `advisory`
フィールドは変わりません。

Route A は**「シグナル源の差し替え」**のリファクタリング (プローブ → `embed()` の結果)
であって、再設計ではありません。利用者から見える契約は同一で、証跡は別建てのプローブ
POST から実際の recall 経路の呼び出しへと*格上げ*され、§4.3 の二重 I/O と、プローブ対
実呼び出しの競合が解消されます。

リポジトリ横断のコストは、実際にはこうなりました。上流に additive なメソッドを足し、
ここへ再 vendor する、それだけです。既存の入口は 1 つも変わらなかったので、他の consumer
が吸収すべきものも、再検証すべきものもありませんでした。

上の見積もりが前提していたことについて、1 つ訂正を残します。ここの vendored copy は
同期元の上流とバイト一致しますが、**その上流はこのクライアントの唯一の系統では
ありません**。

別途保守されている姉妹系統が存在し、上流には無いトランスポートモードと、より広い失敗
捕捉を持っています。上流への additive な変更はそちらには届かず、逆向きに同期すれば
そちらが持つものを消します。canonical な写しが 1 つだけあるかのように作業を計画すると、
変更は呼び出し元の半分を、何も告げずに取りこぼします。

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
3. **プローブの配置** (§4.3 の詳細化、**§6 により置き換え済** — プローブと専用
   タイムアウトは撤去され、失敗経路はプローブ I/O を有界にするための
   `health.is_faulted()` ゲートを必要としなくなりました)。`health.py` は `vector` を
   import しないままなので依存グラフは不変です: `config ← health ← vector ← memory_handlers`。

   観測点は 2 つあり、対称ではありません。recall 経路の embed は失敗と復旧の両方を報告し、
   保守の再埋め込みは失敗のみを報告します。復旧を recall 側に残すのは意図的です — 保守実行が
   状態を消すと、利用者の recall が直前に latch した fault と、どのセッションに既に通知したか
   の記録を消してしまいます。2 つ目の観測点は保守側のブレーカーと共に追加されたもので、
   `vector.py` の外で health に書き込む唯一の場所です。「観測点」を単数で書く doc は、最初の
   ビルドを説明していてコードを説明していません。
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

§3 の項目 4 の発火ルール、および §4.5 / §6 のペイロードのフィールド一覧を supersede
します。

**欠陥。** 「完全版 runbook はもう発火した」はプロセスの状態
(`health._advisory_emitted`) であり、エピソードごと 1 回の縮退はこれを鍵にしています。
stdio ではこれが意図どおりのルールです。1 プロセスが 1 クライアントセッションを相手に
するからです。

しかし `CPERSONA_TRANSPORT=streamable-http` は
`StreamableHTTPSessionManager(stateless=True)` で動くため、1 プロセスが接続中の全
クライアントに応答します。障害中の最初の recall が全員分の完全版 runbook を消費し、
他のすべてのセッションには `FAULT_RUNBOOK_SHORT` が渡っていました。これは
`**Notify the user:**` の命令を持たず、そのセッションが受け取っていないメッセージへの
続報として読めてしまいます。

項目 7 の正直な到達範囲 (「fault は*次の* recall で表面化する」) は、いつのまにか
「あるひとつのセッションの次の recall で、障害ごとに 1 回だけ」になっており、それを
告げるものはありませんでした。

実トランスポート経由で計測しました。1 プロセス・1 回の障害で 2 クライアントが recall
したところ、最初は命令付きの 1067 文字、2 番目は命令なしの 107 文字でした。

**現在のルール。** セッション識別子を申告しない呼び出し側については、プロセスが複数
セッションを相手にしている間、`fault` は縮退しません。障害は稀であり runbook はこの機能
の目的そのものなので、recall ごとにその費用を払う方が、1 つを除く全セッションで沈黙の
費用を払うより良い、という判断です。

`hint` は従来どおり縮退します。`mode=none` は恒久的な状態なので、免除するとフル版の
runbook を recall ごとに永久に繰り返すことになります。埋め込みバックエンド無しでの運用は
障害ではなく継続的な状態であり、都度エスカレーションすべきものではありません。

**どちらのルールが効いているかは、ペイロードに書かれます。** `advisory_scope` は抑制
状態が共有されている時に `"process"`、プロセスがそのままセッションである時に
`"session"` を返します。これによりクライアントは、受け取っていないものへの続報と、
受け取ったものへの続報を区別できます。no-persist のトグルも同じやり方で影響範囲を開示して
います。この advisory はそうせず、自分のペイロードを、何も告げずに劣化させていました。

**その後、セッション単位になりました。** ここが置き換えた段落は、セッション単位の抑制を
到達不能として記録していました。recall の継ぎ目にはセッションを識別できるものがなく、
HTTP モードは stateless なのでリクエストを越えて残るセッションはなく、ACL の principal は
クライアント id しか持たない (1 つの資格情報を共有する 2 つのウィンドウは 1 つの
principal)、というものです。

欠けていたのは、トランスポートが供給する識別子ではなく**呼び出し側が申告する**識別子で
あり、それが出荷されました。セッション鍵を申告した呼び出し側は、そのセッションを鍵とする
抑制を得るので、障害中の各セッションが 1 度ずつ通知されます。`advisory_scope` はそれに
対して `"session"` を返します。先の段落が挙げたフィールドで、予告どおり形は変わって
いません。

上の `fault` 免除は、申告された鍵に対しては意図的に適用しません。免除は識別子の欠如を
補うためのものなので、識別子がある場所で、既に受け取ったセッションにフル runbook を
繰り返せば、免除が避けていた費用をそのまま復活させることになります。何も申告しない
呼び出し側の挙動は、従来のままです。