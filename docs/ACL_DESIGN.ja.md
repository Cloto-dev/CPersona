<!-- i18n-source: docs/ACL_DESIGN.md@blob:04178308043646d2ba45a3d0b34800d1eabe4b61 -->

# クライアント別ケーパビリティ / ACL 設計

> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

**Status**: APPROVED — 2026-08-22 のメンテナ裁定により、§9 の決定点はすべて提案
どおりに解決されました (D4 は修正後の read-write 形で)。この文書が実装契約です。
実装中に見つかった逸脱は §9 に差し戻します。
**Scope**: クライアント別・エージェント別の read/write ケーパビリティを、サーバー
側でハードに強制すること。OAuth ベースの identity はここで定義する継ぎ目 (§3.1)
に差し込まれる別ラインであり、意図的に対象外です。

---

## 1. 問題 { #1-problem }

現在の認証は `CPERSONA_AUTH_TOKEN` ひとつだけです: それを持つクライアントは、
あらゆる `agent_id` に対してあらゆるツールを呼べます — `agent_id` は呼び出し側が
自由に選ぶ普通のツール引数だからです。2 つ目のクライアントを「read-only、かつ
これらのエージェントに限る」として配線する手段はありません — その意図は
クライアント自身の instructions に散文として書くことしかできず、それは自主性
頼みで強制されていません。サーバーが「ノー」と言えなければなりません。

目標とするモデル (例): `client_A = {alpha: read-write, beta: read}`、
`client_B = {beta: read-write}` — どのクライアントが何を送ってこようと、サーバー
側で強制されます。

## 2. 信頼モデル { #2-trust-model }

**防ぐ対象**: 権限過剰、あるいは挙動のおかしい*認証済み*クライアント — バグや
プロンプトインジェクションによる誤った `agent_id`、read-only の意図で配線した
クライアントからの書き込み、クライアント単位での影響範囲の封じ込め。

**防がない対象**: ネットワーク層の露出 (従来どおり: bind ガード + bearer 認証)、
フル権限クライアントのトークン漏洩、stdio トランスポート上の敵対的なローカル
プロセス (§5.4)。共有秘密を超える identity の*証明*は OAuth ラインの仕事であって、
こちらの仕事ではありません。

## 3. 中核モデル { #3-core-model }

```
request ──► IdentityResolver ──► client_id ──► ACL[(client_id, agent_scope)] ──► allow / deny
```

- **Principal** (`client_id`): クライアントを指す不透明な文字列。identity resolver
  がリクエストから解決し、システムの他の部分は資格情報を一切見ず、`client_id`
  だけを見ます。
- **Grant** (権限付与): `(client_id, agent_pattern) → none | read | read-write`。
  `agent_pattern` は厳密な `agent_id` か、ワイルドカード `"*"` です。
- **権限の束 (lattice)**: `none < read < read-write`。`read-write ⊃ read`。
- `(client, agent)` の **実効権限** = 完全一致の grant があればそれ、なければ
  そのクライアントの `"*"` grant、それも無ければ `none`。完全一致は、与える権限が
  *より小さい*場合であってもワイルドカードに優先します — `{"*": "read",
  "noisy": "none"}` と書いた運用者は、その例外を意図していたのです。

### 3.1 identity の継ぎ目 (要となる抽象) { #31-the-identity-seam-the-load-bearing-abstraction }

強制層が消費するのは `client_id` だけです。`client_id` をどう確立するかは、狭い
インターフェースの背後に置かれた差し替え可能な resolver です:

```python
class IdentityResolver(Protocol):
    def resolve(self, request) -> Principal | None:
        """Return the authenticated principal, or None for 401."""
```

- **v1 resolver — 名前つき静的トークン** (§4): bearer トークン → `client_id` の
  対応表。
- **将来の resolver — OAuth**: token introspection / JWT 検証が、*同じ*
  `client_id` 名前空間へ解決します。差し込むだけで済みます: grant テーブルも、
  強制層も、その下のテストもすべて無変更です。

**厳格な制約**: resolver の外側では、identity が静的トークン由来であると仮定して
はなりません。`Principal` に bearer 固有のフィールドを置かない、resolver より先で
`Authorization` ヘッダを読まない。この制約こそが、OAuth ラインを作り直しではなく
加算的なものにします。

## 4. 設定 { #4-configuration }

権威ある保存先は**設定ファイル**であって、データベースではありません。理由: DB に
はブートストラップ問題があること (資格情報がなければ到達できない保存先へ、最初の
管理者資格情報を誰が挿入するのか)、grant 集合は小さく運用者が手で書くもので
あること、そして「ファイル + 再起動」が CPersona の他のつまみすべてと同じ流儀で
あること。DB スキーマも無変更のまま保たれます (2.5.x ラインの不変条件)。

```jsonc
// CPERSONA_ACL_FILE=/path/to/acl.json
{
  "clients": [
    {
      "client_id": "assistant-a",
      "token": "${CPERSONA_TOKEN_ASSISTANT_A}",   // literal or ${ENV} reference
      "grants": { "alpha": "read-write", "*": "read" }
    },
    {
      "client_id": "importer",
      "token": "s3cr3t-literal-also-allowed",
      "grants": { "beta": "read-write" }
    }
  ]
}
```

- `token` はリテラル、または `${ENV_VAR}` 参照を受け付けます (読み込み時に解決。
  未設定の変数は起動時エラー — fail closed、§7)。
- `client_id` の重複、または解決後のトークンの重複: 起動時エラー。トークンの
  参照は曖昧であってはなりません。
- 権限は厳密に `"none" | "read" | "read-write"` のみです。未知の文字列:
  起動時エラー。
- ファイルパーミッション: ファイルがグループ/全体から読める場合、ローダーが警告
  します (DB ファイルと同じ構え)。

### 4.1 2 段階の既定 (後方互換性) { #41-two-stage-default-backward-compatibility }

| 状態 | 挙動 |
| --- | --- |
| `CPERSONA_ACL_FILE` 未設定 (既定) | **レガシーモード。現在の挙動とバイト単位で同一**: `CPERSONA_AUTH_TOKEN` の単一トークン (または認証なし + 到達可能性の警告)、すべての呼び出し側にフルケーパビリティ。ACL のコードは判断を一切下しません。 |
| `CPERSONA_ACL_FILE` 設定あり | **ACL モード**: 名前つきトークンのみ。すべての呼び出しが解決され検査され、明示的に許可されていないものは拒否されます。 |

ACL モードは opt-in であり、既定の挙動は変わりません — このリリースは加算的に
出せます。ACL モードでは、`CPERSONA_AUTH_TOKEN` が併せて設定されていても
**起動時警告つきで無視されます** (提案。代替案は §9-D3): 資格情報の権威は同時に
1 つだけであり、忘れられたレガシートークンが隠れたフルケーパビリティのバック
ドアとして生き残ってはなりません。

## 5. 強制 { #5-enforcement }

2 層あり、どちらもサーバー側です。

### 5.1 レイヤー 1 — トランスポート認証 (`BearerTokenMiddleware` の拡張) { #51-layer-1-transport-authentication-extends-bearertokenmiddleware }

現在のミドルウェアは、トークンを 1 つ比較して (`hmac.compare_digest`) 転送します。
変更点: ACL モードでは、そのトークンをクライアント表に対して解決します —
**すべての**エントリに対する定数時間比較 (早期リターンなし、トークンをキーとした
辞書引きなし。表は小さい) — そして解決した `Principal` を
`contextvars.ContextVar` に格納し、dispatch 層に渡します。未知のトークン、または
トークンが無い場合は、現在とまったく同じく `401` です。

伝播についての注記: HTTP トランスポートは
`StreamableHTTPSessionManager(stateless=True)` で動くため、ツール呼び出しは ASGI
リクエストのタスク系譜の内側で実行され、ミドルウェアが設定したコンテキスト変数は
dispatch 時に見えます。これは実装詳細への依存なので、専用の配線テスト (§8) を
持たせます — 将来トランスポートが変わったとき (セッションありモード、タスク
プール) に、すべての呼び出しが黙って「principal なし」に解決されるのではなく、
赤で落ちるようにするためです。

### 5.2 レイヤー 2 — ツール dispatch でのケーパビリティ検査 { #52-layer-2-capability-check-at-tool-dispatch }

vendored された `ToolRegistry.call_tool` の継ぎ目は触りません — これは複数の
サーバーで共有されており、consumer をまたぐ変更は別の (そしてはるかに高価な)
リリース類だからです。代わりに cpersona は、`server.py` の登録時に自前の
ハンドラをラップします: 29 個の `auto_tool` 登録の後、1 回のパスで各ハンドラを
次のものへ置き換えます。

```
guard(tool_name, handler):  arguments → resolve scope → check → handler | denial
```

この検査は `(principal, tool_classification[tool_name], agent_scope(arguments))` を
読み、grant テーブルと突き合わせます。fail-closed な既定: レジストリには存在する
が分類表に無いツールは、ACL モードでは明示的な「unclassified tool」エラーとともに
**拒否**されます — 新しいツールが強制層から見えないまま出荷されることはあり得ま
せん (§8 が網羅性テストでこれを固定します)。

### 5.3 エラー契約 { #53-error-contract }

- トランスポート層の失敗は HTTP のままです: 現在と同じく `401` (不正な、または
  欠けたトークン)。
- ケーパビリティの拒否は**ツールエラー**です (MCP 呼び出しはトランスポート上は
  成功し、構造化された拒否を返します)。判断にはツール引数が必要で、dispatch の
  内側で起こるからです:

```json
{ "ok": false, "error": "permission_denied",
  "tool": "store", "agent_id": "beta", "required": "read-write",
  "client_id": "assistant-a" }
```

上の形は最小であって最大ではありません: 既存フィールドが運んでいない原因を guard
が名指しできるときは、`detail` 文字列がこれに加わります — 未分類のツール、文字列
でない scope 引数、あるいは scope をまったく送らなかったために全エージェントを
要求した呼び出し、といった場合です。例そのもののケースでは意図的に不在です。
そこでは `agent_id` と `required` が「どのエージェントで、どの水準が足りなかったか」
をすでに述べており、それを散文で言い直しても情報は増えないからです。

`client_id` は呼び出し側自身の解決済み identity を返しているだけです (自分自身に
とっては秘密ではありません) — 配線を誤ったクライアントを、その側から診断できる
ようにするためです。拒否はサーバー側でも同じフィールドとともに WARNING で記録
されます (可観測性のため。本物の監査ログがいつか欲しくなったときの種でもあります
— 今は意図的に作りません)。

### 5.4 stdio トランスポート { #54-stdio-transport }

stdio トランスポートには解決すべき資格情報がありません — 相手はプロセスを起動した
者そのものです。提案: ACL モードでは、stdio は予約 principal `"local"` に解決
します。その grant は同じファイルから来ます (`"client_id": "local"`、`token`
フィールドなし)。`"local"` が無ければ、stdio 呼び出しは権限の無い他の principal と
同様に拒否されます — ACL モードを有効にする運用者は、ローカルのものも含め、
すべての principal を明示的に述べるからです。レガシーモードでは、stdio は現在と
同じく無制限です。

## 6. ツール分類とエージェントスコープの解決 { #6-tool-classification-and-agent-scope-resolution }

分類は cpersona のコード内にある明示的な表 (`ACL_CLASSIFICATION`) であって、実行時
に `ToolAnnotations` から導出するものではありません。annotations はクライアント
向けのヒントであり、ACL 表はサーバー側のセキュリティ判断です。両者はソースを
共有するのではなくテスト (§8) によって互いに正直さを保ちます — 食い違ったときは、
別の目的で書かれたヒントを黙って継承するのではなく、テストがその食い違いを検討
させます。

調査結果 (`server.py` の登録から、29 ツール全数): 現在 `ToolAnnotations` を
**持たない**ツールが 2 つあります — `calibrate_threshold` と
`set_recall_precision` です (どちらも較正状態を変更します。この欠落は §9-D6 が
どちらに決着するかとは独立に、実装と併せて修正します)。

| ツール | ケーパビリティ | エージェントスコープ |
| --- | --- | --- |
| `recall`, `recall_with_context`, `get_contents`, `get_profile`, `list_memories`, `list_episodes`, `get_recall_precision` | read | `agent_id` 引数 |
| `store`, `update_profile`, `archive_episode`, `update_memory`, `lock_memory`, `unlock_memory`, `delete_memory`, `delete_episode`, `delete_agent_data` | read-write | `agent_id` 引数 |
| `calibrate_threshold`, `set_recall_precision` | read-write | `agent_id` 引数 (エージェント別の較正状態を変更する) |
| `check_health`, `deep_check` | read。**`fix=true` のときは read-write** | `agent_id` 引数。空 = 全エージェント → `"*"` に対する grant が必要 |
| `migrate_channel_axis` | read-write | `agent_id` 引数。空 = 全エージェント → `"*"` |
| `export_memories` | read-write。**`CPERSONA_EXPORT_DIR` が未設定のあいだ (出荷時の既定) は、要求が `"*"` に格上げされます** — パス引数はファイルシステム上のどこでも呼び出し側が選べるため、影響範囲は 1 エージェントのデータでは済みません (§9-D4、マージ前レビューによる 2 つ目の修正) | `agent_id` 引数 |
| `import_memories` | read-write。`CPERSONA_EXPORT_DIR` が未設定のあいだは同じ `"*"` への格上げ | `target_agent_id`。空 = 「ファイルに記録されたとおり」→ `"*"` |
| `merge_memories` | `copy`: read(source) + read-write(target)。`move`: **両方**に read-write (move は source の行を削除するため) | `source_agent_id` + `target_agent_id` |
| `pause_persistence`, `resume_persistence` | `"*"` に対する read-write (プロセス全体のスイッチであり、全エージェントの書き込みに影響する) | global |
| `persistence_status`, `get_queue_status`, `get_operating_context` | スコープなしの read: **認証済みの principal すべて**に許可 (エージェント別のデータを持たないため。§9-D5) | none |

この表が依拠している解決規則:

- scope が `"*"` に解決される呼び出し (全エージェント対象ツールでの空の
  `agent_id`) は **sweep** です: 例外として指定されたものも含め、全エージェントに
  触れます。これが満たされるのは、すべての grant 行が許す水準においてのみ —
  ワイルドカードの grant と、名前つきの例外すべてにわたる最小値です。名前つきの
  エージェント 3 つに `read` を持っていても、それが `"*"` に対する `read` に
  積み上がることはありません。また `{"*": "read-write", "prod": "none"}` は、
  全エージェント呼び出しを通じて `prod` に到達できません: 運用者はその例外を
  意図していたのです (sweep に適用した D6。マージ前レビューによる改良 — 最初の版は
  ワイルドカードの grant しか参照しておらず、名前つきの `"none"` が禁じたことを
  sweep に許してしまっていました)。
- 空でない文字列ではないエージェントスコープ引数 (欠如、空、または文字列以外の型)
  は、ワイルドカードの要求 — 最も広い要件 — に解決されます。guard はツールごとの
  パラメータ検証の外側で走るため、検証済みの形を仮定してはなりません (マージ前
  レビューによる改良)。
- ファイル I/O の格上げが検査するのは `CPERSONA_EXPORT_DIR` が設定されているか
  どうかだけであり、それがどれだけの範囲を含むかは見ません。**サービス専有の
  ディレクトリを指してください**: 広い root (`/home`、`/var`) を指すと、要求は
  エージェントスコープに戻る一方で、呼び出し側は依然その内側のほぼ任意のパスを
  選べてしまい、格上げが閉じたものを黙って開き直します。
- 条件つきのケーパビリティ (`fix=true`) は、ハンドラが走る**前**に引数から解決
  されます。guard は、ハンドラが見るのと同じ検証済みの引数を見ます。

## 7. 失敗時の構え { #7-failure-posture }

最も早い継ぎ目で、fail closed に、そして声高に:

- 不正な形式の ACL ファイル、未知の権限文字列、トークン/クライアントの重複、
  解決できない `${ENV}`: **起動時エラー** — 書かれたものとは別のポリシーで提供する
  くらいなら、サーバーは提供を拒みます。
- ACL モードの dispatch で principal を解決できない場合 (contextvar が空 — 配線の
  リグレッション): 拒否 + ERROR ログ。「principal が無い、ゆえに制限も無い」には
  決してしません。
- ACL モードで分類表に無いツール: 拒否 (§5.2)。

## 8. テスト戦略 { #8-test-strategy }

1. **resolver の単体テスト**: 表に対する token→principal、定数時間の経路、未知の
   トークン → `None`、`${ENV}` の解決とその失敗。
2. **配線テスト** (既存の `test_253_middleware_wiring.py` のパターンを拡張 —
   組み立て済みアプリに実際の ASGI リクエストを通す): 未知トークンで 401、許可
   された呼び出しが通ること、*同じ*呼び出しがより小さい grant の下で §5.3 の拒否を
   返すこと — ミドルウェア → contextvar → guard を端から端まで固定します。これは
   `stateless=True` の伝播の証明も兼ねます。
3. **分類の網羅性**: `registry._handlers` にある名前がすべて
   `ACL_CLASSIFICATION` にあること。登録済みだが未分類のツールはスイートを落と
   します (かつ実行時には拒否されます — fail-closed の両半分)。
4. **annotations との相互照合**: `readOnlyHint=True` ⇔ 分類 `read`。レビュー済みの
   明示的な例外リストつき (想定: `check_health` / `deep_check` — 書き込みモードを
   持つ read ツール)。
5. **scope の解決**: merge の move/copy の grant 行列、空の `agent_id` → `"*"`、
   `fix=true` による格上げ。
6. **レガシーモードとの等価性**: `CPERSONA_ACL_FILE` 未設定では、guard は判断を
   一切下さないこと — 挙動が同一のスイート実行。
7. **stdio の principal**: §5.4 に従い、`"local"` があれば許可 / 無ければ拒否。
8. **変異検査** (リリースゲートの規律): guard のラップを外す → 赤。grant を 1 つ
   反転する → 赤。分類の行を 1 つ削る → 赤。

## 9. メンテナ向けの決定点 { #9-decision-points-for-the-maintainer }

提案する既定は §§3–7 が規定するものです。いずれも実装前であれば安く変更できます。

| # | 決定 | 提案 | 代替案 |
| --- | --- | --- | --- |
| D1 | grant の保存先 | 設定ファイル (§4) | DB テーブル (ブートストラップ問題。そもそもこのラインではスキーマ変更は選択肢外) |
| D2 | 既定のポリシー | 2 段階の opt-in (§4.1) | ACL を常時 on にし、暗黙のフル権限デフォルトクライアントを置く (レガシー配備で可動部が増える一方、強制力の得は何もない) |
| D3 | ACL モードでの `CPERSONA_AUTH_TOKEN` | 無視 + 起動時警告 | 組み込みのフルケーパビリティクライアントへ自動マッピング (隠れたスーパートークンを 1 つ生かし続ける — この機能が取り除くために存在する、まさにその失敗モード) |
| D4 | `export_memories` | read-write。元の「read」の論拠 (「ファイル書き込みは運用者が構成するものであって、呼び出し側が選ぶものではない」) は `_confine_io_path` との接触に耐えません: `CPERSONA_EXPORT_DIR` が**未設定 — 出荷時の既定 — の場合、呼び出し側は `..` を含まない任意の絶対パスを選べます** (bug-054 の系譜)。さらに既存の annotation はすでに `destructiveHint=True` を宣言しています | read。ただし `CPERSONA_EXPORT_DIR` による封じ込めが実際に構成されているときにのみ有効 — read-only のバックアップクライアントが本当に必要だと判明したときの、条件つきの後日緩和 |
| D5 | スコープなしの read (`persistence_status` など) | 認証済みの principal すべて | `none` でない grant を少なくとも 1 つ要求する (権限を完全に剥奪されたが表には残っているクライアントから、ステータスの可視性さえ奪う) |
| D6 | ワイルドカードの意味論 | 完全一致がワイルドカードを両方向で上書きする (§3) | ワイルドカードを下限とする (`max(exact, "*")`) — より単純だが、「どこでも read、ただし X だけ none」を表現できない |
| D7 | 出荷の形 | alpha を 1 段 (認証面の変更。resolver + guard を実トラフィックで慣らす) | final へ直行 (形式上は許容: 加算的、既定 off) |

## 10. 非目標 { #10-non-goals }

- **OAuth / identity の証明** — 別ライン。§3.1 を利用します。
- **project_id / channel の粒度** — 現在 consumer がいません。grant の値は閉じた
  列挙なので、後日の `{perm, projects: [...]}` 拡張は加算的です。
- **レート制限、クォータ、監査の永続化** — 対象外。§5.3 のログはその種であって、
  機能そのものではありません。
- **agent_id を超えるマルチテナンシー** — エージェント軸が分離モデルです。
