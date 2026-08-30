<!-- i18n-source: docs/SESSION_IDENTITY_DESIGN.md@blob:5bb921c0aa1e7e4b9a39bb3d85d1d93afb6d3997 -->

# 申告型セッション同一性 (`session_key`)

> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

ステータス: 2.5.7b1 ラインへの提案。加算的かつ挙動保存 — 何も送らない呼び出し元に
とっては、今日の挙動がバイト単位で変わりません。

## 1. 問題: プロセスはセッションではない { #1-the-problem-a-process-is-not-a-session }

stdio トランスポートでは 1 プロセスが 1 クライアントを捌くため、プロセス global な
状態は構造的にセッションスコープになります。streamable-HTTP ではそうなりません。
サーバーは `StreamableHTTPSessionManager(..., stateless=True)` で走り、1 プロセスが
接続中の全クライアントに応答し、リクエストをまたいで生き残るセッションが存在しません。
`config.shared_transport()` は既にその条件に名前を与えており、呼び出し側が何度も同じ
問いを立てたために存在しています。

バグ台帳にある 2 件の欠陥は、同じ「軸の欠落」が 2 度現れたものです。そして両方とも、
正直さが許す範囲までしか修理されていません:

- **bug-151** — `pause_persistence` は素のプロセス global フラグです。あるクライアント
  の pause が、TTL が切れるまで**他の全接続セッションの write を黙って no-op に
  変えます**。修理は pause / resume / status の応答に `scope: "process"` を足し、
  per-session スコープを偽って示唆していた docstring を訂正しました。
  **per-session 状態は追加していません**: 影響範囲は開示されただけで、除去されて
  いません。
- **bug-251** — 劣化 recall の advisory は「利用者に知らせよ」というフル runbook を
  *プロセス*ごとに 1 回だけ発火します。そのため障害中に知らされるのは 1 セッション
  だけで、他のセッションは自分が見ていないメッセージへの続報を受け取ります。修理は
  抑制を transport 単位に付け替え、なぜそれ以上進めなかったかを注記しています —
  per-session の抑制は*「caller-supplied key が要る」*と。`health.py` も同じことを
  未来形で述べています。`advisory_scope` は「caller 由来の key がこの継ぎ目に届いた日」
  に `session` になる、と。

3 つ目の面は既にこのパラメータを持っています。`get_session_findings` が `session_key`
を受け取るのは、SuperAuditor 標準 §7 が「セッションを見分けられない実装はそう言え」と
要求するためで、その docstring は key が「正直さのフラグ以外は何も変えない」と認めて
います。

本設計が想定する配備では、クライアントはこの同一性を帯域外で供給できません。環境変数は
プロセス起動時に確定し、ヘッダ値は起動時に一度だけ展開され、トランスポートのセッション
id は後続の呼び出しでサーバーへ返ってきません。**同一性は呼び出しの引数、すなわち
データとして運ぶしかない** — これは CScheduler が自前の `session_key` を足す前に到達した
結論と同じです。

## 2. これが何であり、決して何になってはいけないか { #2-what-this-is-and-what-it-must-never-become }

`session_key` は**不透明で、クライアントが申告する分割ヒント**です。サーバーはその
バイト列に意味を与えません — 比較するだけで、それ以外は何もしません。

これは**認証ではありません**。どの呼び出し元も任意の文字列を送れます (他セッションの
ものを含む)。クライアント別ケーパビリティの線とも OAuth とも直交します。あちらは呼び出しが
*許されるか*を決め、こちらは許された呼び出しが*どのプロセス内バケット*に落ちるかを決めます。

これは**第 4 の分離軸ではありません**。`agent_id` / `project_id` / `channel` はクエリが
*誰のデータ*を読むかを選びます。`session_key` は呼び出しが*誰のプロセス内状態*に触れるかを
選びます。この key で DB の行がフィルタされることはなく、保存済みの記憶が到達可能に
なったり不能になったりもしません。この線を最初に引くのは、次に必ず来る要望 —
「recall をこのセッションに絞りたい」 — が、記憶を**それが越えるために存在する境界**の
向こうで読めなくしてしまうからです。それを望むなら、上記 2 件の欠陥とはまったく別の
論拠が要ります。

## 3. 継ぎ目 { #3-the-seam }

解決点は 1 つだけで、姉妹実装と同じ形です:

```python
def resolve_session_key(declared: str | None) -> tuple[str, bool]:
    """Return (effective_key, declared)."""
    if isinstance(declared, str):
        stripped = declared.strip()
        if stripped:
            return stripped, True
    return TRANSPORT_KEY, False
```

受け取るのはリクエストではなく値です。本サーバーのツールレジストリは宣言済みパラメータを
ディスパッチ前に検証・抽出するため、ハンドラはキーワード引数を受け取り、引数 dict を見る
ことがありません (姉妹実装が dict から解決するのは、あちらのディスパッチが dict を渡すから
です。継ぎ目は同じで、宿主が違います)。

- 空白のみでない非空文字列が実効 key となり、`declared` は true になります。
- 不在・空・空白のみは transport fallback に落ち、`declared` は false —
  既存の全呼び出し元にとって今日の挙動そのままです。
- 長さ制限も、書式検証も、`strip()` を超えるサニタイズもありません。値は比較される
  だけで、パースされず、SQL 識別子に展開されず、同一性の主張としてログにも残りません。

fallback はプロセス単位の定数 (`TRANSPORT_KEY`) です。stdio ではプロセスが 1 セッションなのでそれ自体が
既にセッションであり、streamable-HTTP では key なしの呼び出し元が全員落ちる 1 つの
共有バケットになります — つまり今日の世界そのものです。fallback が**プロセスから
セッションを導出しようとしない**のは意図的です。共有トランスポートではプロセスの系譜は
サーバー自身を指すのであって呼び出し元の誰でもなく、そこから導いた key は答えのように
見えて何も分割しません。

呼び出し元が fallback の値そのものを申告してそのバケットに入ることは当然できます。これは
塞ぐべき穴ではありません — key はヒントであり、ある key を送れる呼び出し元は別の key も
送れます。これによって守られているものは何もありません。

## 4. 何を分割するか — 完全な一覧 { #4-what-it-partitions-the-complete-list }

想定ではなく実測です。パッケージ内のモジュールレベルの可変値をすべて列挙し、プロセス
global **かつ**セッション形状であるものは以下の 2 つだけでした:

| 状態 | 現在 | key 申告時 |
| --- | --- | --- |
| `no_persist` の pause フラグ (`_no_persist_until`) | プロセス全体で 1 つ | key ごとに 1 エントリ |
| 劣化 advisory の抑制 (`health._advisory_emitted`) | プロセス全体で 1 つ | key ごとに 1 エントリ |

候補に見えた他のものは、いずれも候補ではありません:

- vector モジュールの閾値・融合ゲート・beta のキャッシュは **agent** をキーにしており、
  calibration authority も同様です。ある agent の較正はその agent のセッション群で
  共有されるべきものです。
- recall precision と profile の状態は DB に agent 単位で保存されています。
- DB のロックがプロセス単位なのは、DB がプロセス単位だからです。

分割対象の 2 つはどちらもプロセスメモリ上にあります。**テーブル変更なし、migration なし、
GC ジョブなし。** ここが姉妹実装との実質的な差です。あちらは分割対象が永続状態だった
ため、3 つのテーブルに `session_key` 列とスキーマ migration が必要でした。

## 5. 段階 { #5-staging }

このパラメータは無料ではない (§6) ので、まず元が取れるところに入れます。

### 段階 1 — advisory の per-session 化と、pause の開示 { #stage-1-the-advisory-and-disclosure-for-the-pause }

`session_key` を受け取るツール: `recall` / `recall_with_context` /
`pause_persistence` / `resume_persistence` / `persistence_status`。

- **advisory が本当に per-session になります。** 抑制が実効 key をキーにするため、
  障害中は recall する各セッションがそれぞれ 1 回ずつフル runbook を受け取り、key を
  申告した呼び出し元に対して `advisory_scope` は `process` ではなく `session` と答えます。
  bug-251 の先送りされた半分はここで閉じます。
- **pause が所有者を開示します。** pause は自分を張った key を記録し、
  `persistence_status` は「呼び出し元自身の key が張ったのか、他セッションが張ったのか」を
  報告します。継承した pause で skip された write もそう言えます。write は依然として
  全体が止まります — これは開示であって分離ではなく、doc がそれ以上を主張してはいけません。

### 段階 2 — pause の per-session 化 { #stage-2-the-pause-becomes-per-session }

`no_persist.is_paused()` を参照する全ツールが自分の key を知る必要があり、それは write 面
への `session_key` の threading を意味します (`store` / `archive_episode` /
`update_memory` / `lock_memory` / `unlock_memory` / `delete_memory` /
`delete_episode` / `delete_agent_data` / `update_profile` / `calibrate_threshold` /
`set_recall_precision` / `migrate_channel_axis`、加えて `export_memories` /
`import_memories` / `merge_memories`)。

key なしの呼び出し元は 1 つのバケットを共有し続けるため、key なしの pause は key なしの
呼び出し元すべてを止め続けます — 構造的に挙動保存です。

段階 2 は段階 1 の実測コスト (§6) を条件とし、既定では予定しません。

### 対象外 { #out-of-scope }

セッション単位の findings probe — 「このセッションが触って pending のまま残した記録」、
SuperAuditor 標準 §7 が想定し姉妹実装が持っているもの — は*どのセッションが行を書いたか*の
記録を要し、それは保存列と migration と保持期間の問題になります。本設計には含めません。
それができるまで `get_session_findings` は key なしの remote 呼び出し元に
`identity_shared: true` を返し続け、他は何も変わりません。

## 6. パラメータ自身のコスト { #6-the-cost-of-the-parameter-itself }

`session_key` を受け取るツールは、その説明文をクライアントが毎セッション読み込むツール
一覧に載せます。これは key を一度も申告しない呼び出し元も含めた**全員が払う固定費**で
あり、本設計が一気に threading せず段階に割る理由です。

本サーバーが広告するツール一覧 (シリアライズ後) の実測値:

| | ツール数 | シリアライズ後の文字数 |
| --- | --- | --- |
| 段階 1 前 | 30 | 39,135 |
| 段階 1 後 | 30 | 41,703 |
| 差分 | 0 | **+2,568 (+6.6%)** |

key を受け取るツール 1 本あたり約 514 文字で、これを全クライアントが毎セッション払います
(key を一度も申告しないクライアントも含む)。説明文は既に 1 つを 5 スキーマで共有しており、
上の数値はその共有版であって、長い文章の 5 コピーではありません。

段階 2 はさらに約 15 本へ key を足すので、上に約 +7,700 文字が乗り、合計差分は +26% 近くに
なります。段階 2 が正当化しなければならないのは、好みではなくこの見積りです。正当化
できない場合の緩和策は、説明を本ページ参照の短文に置き換えて 1 ツールあたり約 350 文字を
削ることですが、それはクライアントから見えないページとの交換になります。

## 7. 寿命 { #7-lifetime }

分割対象の 2 つはどちらも永続行ではなく、上限付きのプロセス内マップです。

- pause は既に TTL を持ち遅延クリアされます。key 単位でも同じ TTL が適用され、
  期限切れのエントリは消えます。
- advisory の抑制エントリは追い出し上限で抑えます。key 空間はクライアント供給であり、
  key をローテーションするクライアントがマップを無制限に育ててはならないためです。
  追い出しが忘れるのは「そのセッションには既に伝えた」ことだけなので、最悪ケースは
  通知の重複 — 安全側です。
- プロセス再起動で両方消えます。これは正しい挙動です。再起動をまたいで生き残る
  セッションは存在しません。

ディスク上に GC すべきものは無く、key が DB に届くこともありません。

## 8. 縮退の契約 { #8-degradation-contract }

key を申告しない呼び出し元にも、推測ではなく事実を伝えます。ただし**何を伝えるかは、その
応答が既に何を言っているかで決まります** — key なしの応答は形を変えてはならないからです
(新しいキーを 1 つ足すだけで §3 が約束する保存が壊れます):

- `get_session_findings` は従来どおり、共有トランスポート上で key なしの呼び出し元に
  `identity_shared: true` を載せます (SuperAuditor 標準の要求。このフィールドは本設計より
  前から存在します)。
- pause の 3 ツールは既に `scope: "process"` と答えており、影響範囲を正確に述べています。
  所有者フィールド (`pause_owner_known` / `paused_by_self`) は **key を申告した呼び出し元に
  だけ**現れ、key なしの応答はバイト単位で従来のままです。
- recall の応答は `advisory_scope` で regime を伝えますが、それは advisory の内側、つまり
  recall が劣化している時にだけ現れます。健全な key なし recall は何も増えません — そこが
  要点です。

stdio では共有の同一性を主張するものは何もありません。プロセスがセッションそのものであり、
それ以外を言うのは虚偽になります。

## 9. バージョンの置き場所 { #9-version-placement }

本件は **2.5.7 beta 系列**に載ります。ラインは `2.5.7b1` で alpha から beta へ昇段し、
その b1 が運ぶのは findings の pull ツールです。本設計の段階 1 は同系列の次のリリースで
あって、`b1` そのものではありません。

いずれにせよ論拠は同じです。加算的・挙動保存・スキーマ変更なしであり、それはリリース
ライフサイクル標準が Current tier のライン内で許容するものです (§2.6 — 次のラインまで
待つのはロールバックできない変更)。pre-release 系列の到達点は `2.5.7` final で、別の番号へ
飛ぶことはありません。

## 10. テスト { #10-tests }

- 解決: 申告あり / 不在 / 空 / 空白のみ の 4 ケースで、返る key と `declared` フラグの
  両方を検査する。
- 挙動保存: 既存の key なし呼び出し列が、両トランスポートで前後同一の応答を返す。
- advisory: 1 回の障害中に異なる 2 つの key がそれぞれフル runbook を受け取る。同一 key の
  2 回目は短縮形になる。key なし remote の 2 者は今日の transport スコープの挙動を保つ。
- pause の開示: `persistence_status` が自 key の pause と他 key の pause を区別する。
  他者の pause で skip された write がそう言う。
- 正直さ: key なし remote の応答は `identity_shared: true` を持ち、stdio の応答はそもそも
  持たない。
- 変異証明: `strip()` の除去・空文字ガードの除去・追い出し上限の除去が、それぞれ欠陥を
  名指しするテストで落ちる。
