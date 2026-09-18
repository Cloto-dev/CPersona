<!-- i18n-source: docs/ASSOCIATIVE_MEMORY_DESIGN.md@blob:cd9eee42107ceb3133b68a49b85a245c135f3fd5 -->
# 連想記憶 — 設計 { #associative-memory-design }

状態: 2.6 系。宣言 (§1–2) と §3 の再構成の段は実装済みで、`traverse` (§4) はまだです。
実装が決める必要のあった点は §9 に記録します。このページは宣言されたグラフ層 —
別名を持つ登録語と、エージェントが主張する関係 — と、それを最初に読む 1 箇所
([再構成想起](RELIABLE_RECALL_2_6.md#7-reconstructive-recall-the-exit)) を固定します。
`recall` が返すものは変わらず、何も宣言されていなければ何も変わりません。

## 0. 何を足し、どこで働くか { #0-what-this-adds-and-where-it-acts }

recall には検索経路が 2 本あります。確率的なベクトルのアームと、表層の字面を照合する
字面のアームです。どちらも同じ種類の問いを取りこぼします — 答えが、問いの語や意味が
直接届くどの記録からも 2 つ先にある問い、あるいは記録とは別の名前で物を呼んでいる問い。
対話ベンチマークで測ると、multi-hop と open-domain の質問型が融合順位の損失が最も大きい
座標です。

連想記憶は**構成上決定論的な**第 3 の経路です。エージェントが entity・その別名・
主語–述語–目的語の関係を宣言し、サーバは宣言を verbatim に保存して SQL と純関数で辿ります。
サーバ境界のどちら側でもモデルは呼びません: 抽出はエージェントの仕事、保存と走査はサーバの
仕事です。このストアで試した fuzzy な拡張はすべて contamination ベンチマークで退行したので、
他の 2 アームが近似であるところで連想だけは厳密です。

最初の段階は意図して狭くしています:

| | この設計 | 別途決めること |
|---|---|---|
| entity・別名・関係を保存し、辿る | する | |
| 再構成想起がそれを読む (cue・束ね・根拠・roles) | する | |
| 呼び出し側が entity の近傍を直接引ける | する (`traverse`) | |
| `recall` が自分の行をグラフで広げる | **しない** | する — §7 |
| entity が自前の安定した埋め込みを持つ | **しない** | 2.6.1 — §7 |
| サーバが教えられていない関係を推論する | **しない** | 後の別層 — §7 |

最初の読み手に再構成想起を選んだのは、その契約に席が既にあるからです: 宣言された関係で
結ばれた行の束ねキー、恒等だった bounded relation walk、そして 6 語のうち 4 語が宣言された
関係を待っている role 語彙。契約は 1 つも変わらず、恒等写像だった段が仕事を始めます。

## 1. スキーマ { #1-schema }

新しいテーブル 4 つ。`memories` と `episodes` は変わりません。すべての行は memory と同じ
3 本の分離軸 (`agent_id`, `project_id`, `channel`) を持ち、読み取りは recall と同じ規則に
従います: エージェントは他のエージェントのグラフを見ず、project bucket は global pool と
一緒に読まれます。

```sql
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    project_id  TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,             -- 正規名、宣言されたまま
    normalized  TEXT NOT NULL,             -- 正規化後の名前 (§2)
    declared_by TEXT NOT NULL,             -- 'agent' | 'operator'
    created_at  TEXT NOT NULL,
    UNIQUE (agent_id, project_id, channel, normalized)
);

CREATE TABLE entity_aliases (
    entity_id   INTEGER NOT NULL,
    alias       TEXT NOT NULL,
    normalized  TEXT NOT NULL,
    PRIMARY KEY (entity_id, normalized)
);
-- 正規化した別名 1 つは、1 スコープ内で高々 1 つの entity に解決する。この表は
-- スコープ列を持たないので、その規則は制約でなく宣言ハンドラが強制する。
-- 索引は照合のためだけにある。
CREATE INDEX idx_entity_aliases_normalized ON entity_aliases (normalized);

CREATE TABLE entity_mentions (
    entity_id   INTEGER NOT NULL,
    ref         TEXT NOT NULL,             -- 'mem:<id>' または 'ep:<id>'
    declared_by TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (entity_id, ref)
);

CREATE TABLE relations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    project_id   TEXT NOT NULL DEFAULT '',
    channel      TEXT NOT NULL DEFAULT '',
    subject_kind TEXT NOT NULL,            -- 'entity' | 'mem' | 'ep'
    subject_id   INTEGER NOT NULL,
    predicate    TEXT NOT NULL,            -- 正規化済み (§2)
    object_kind  TEXT NOT NULL,
    object_id    INTEGER NOT NULL,
    anchor_ref   TEXT NOT NULL DEFAULT '', -- 根拠となる記録、無ければ ''
    declared_by  TEXT NOT NULL,
    declared_at  TEXT NOT NULL,
    UNIQUE (agent_id, project_id, channel,
            subject_kind, subject_id, predicate, object_kind, object_id, anchor_ref)
);
```

2 種類の関係が 1 つのテーブルを共有し、端点の種類で区別されます:

- **entity → entity** (`Kirari` *maintains* `mizeye`): walk が辿るもの。
- **record → record** (`mem:<newer>` *corrects* `mem:<older>`): 2 つの候補を 1 つの item に束ね、
  role 語彙が読むもの。

混合の端点 (entity と record の関係) は受理して保存しますが、この段階では読みません。

このデータベースは外部キーを使わないので、溢れ分の tree と同じく整合性はトリガーで
保ちます: entity の削除はその別名・言及・関係を削除し、memory / episode の削除はその言及と、
それを端点または anchor に持つすべての関係を削除します。根拠が消えた宣言は一緒に消えます —
関係が、もう存在しない行を指したまま残ることはありません。

## 2. 宣言 { #2-declaring }

宣言の出どころは 2 つで、サーバはどちらかを記録します: 下のツールを通じたエージェントか、
dashboard を通じた運用者か。サーバが第 3 の出どころになることはありません — 保存された
テキストから entity を抽出せず、既存の 2 つから関係を推論しません。したがってカバレッジは
宣言されたものそのものであり、それがこの層の取引です: 他の 2 アームが広く近似であるところで、
狭く確実。

**正規化**は宣言が受ける唯一の処理で、保存形に対しては無損失です: 宣言された `name` /
`alias` / `predicate` は書かれたまま保持し、`normalized` の双子 — Unicode NFKC、
case-fold、空白を 1 つに畳んで trim — を照合に使います。正規化して一致する 2 つの宣言は
1 つの entity です。トークナイザも固有表現認識も使いません: 日本語の固有名詞を決定論的に
切る方法は存在しないので、サーバはそれを試みません。

**`store` への相乗り。** memory を保存する瞬間が、エージェントがそれの言及するものを
知っている瞬間です。`store` は任意の `associations` オブジェクトを受け取ります:

```jsonc
"associations": {
  "entities":  [{ "name": "MizEye", "aliases": ["mizeye", "ミズアイ"] }],
  "relations": [{ "subject": "Kirari", "predicate": "maintains", "object": "MizEye" }]
}
```

ここで名指された entity は未登録なら登録され、保存された memory がそれぞれを言及するものと
して記録されます。関係は端点を entity 名か記録の ref (`mem:<id>`) で名指します; 未知の entity を
名指す端点も登録されます。保存された memory がその関係の `anchor_ref` になります。associations が
妥当かどうかにかかわらず memory は保存されます: 不正な宣言は応答で報告して落とすだけで、
memory を失う理由にはなりません。

**単独。** `declare_associations` は同じオブジェクトと任意の `anchor_ref` を受け取り、
後からの宣言や dashboard からの宣言に使います。加えて `retract` — 取り消す関係 id と言及の
対のリスト。取り消しは宣言がストアを離れる唯一の経路で、自動失効はなく、誤った宣言は
取り消されるまで誤ったままです。

## 3. グラフが読まれる場所: 再構成想起 { #3-where-the-graph-is-read-reconstructive-recall }

再構成想起には 4 段があります。グラフはそのうち 3 段に入り、4 段目の構造化は変わりません。

**段 1 — 候補。** 検索の前に、問いをスコープ内の別名と照合します (正規化した部分文字列一致、
長い別名が先)。問いが言及する entity ごとに、その正規名と他の別名を**字面のアームにだけ**
追加語として渡します。ベクトルのアームは問いをそのまま受け取ります: 別名を付け足すと問いの
埋め込みが動き、展開の目的は別の名前を使った記録を見つけることであって、問いの意味を変える
ことではないからです。候補の深さ (`top_k`) は変わりません; 一致する別名が無ければ、検索は
今日とまったく同じです。

**段 2 — 束ね。** 新しいキー `cluster:relation` が、宣言された record → record 関係で結ばれた
2 つの候補を結びます。キー順では `cluster:adjacent` の後です。言及する entity を共有することは
束ね**ません**: そうするとプロジェクト名を言及するすべての memory が 1 つの item に畳まれ、
この層が再導入してはならない contamination そのものになります。

**段 3 — bounded relation walk。** 検索が直接浮かせた候補ごとに、
walk は次を辿ります: 候補が言及する entity; その上に宣言された entity → entity の関係を
`max_hops` まで; 到達した entity を言及する記録。walk が到達した記録は、そこへ導いた直接
候補の item の**内側の根拠**になります — 新しい item には決してなりません。各記録は
`why: "relation:<predicate>"` とホップ数を持ちます。walk は `max_hops` で止まり、落としたものは
根拠上限と同じく `bounds.omitted` で報告します。到達した記録が `max_evidence` の許す数を超える
時にどれを残すかは、書かれた 1 つの順序で決まります: ホップ数が少ない順、次に最近宣言された
関係、次に記録 id の小さい方。

**Roles。** 述語が role 語彙 (`supports`, `supersedes`, `corrects`, `qualifies`, `contradicts`,
`temporal_predecessor`) の語である record → record の宣言関係は、語彙が既に固定している向き
(参照される行が主語) でその role として出力されます。それ以外の述語の関係も束ね、`why` で
自分を説明します; ただ role ではないだけです。

walk は 1 つの関数 — 候補プールとスコープ内の関係が入り、出自付きの拡張プールが出る — で、
1 回の再構成につき正確に 1 回呼ばれます。この形が、後で §1 の想起プロセスが同じ関数を
反復ごとに 1 回呼ぶことを可能にします (§6)。

## 4. `traverse` { #4-traverse }

記録ではなく entity が欲しい問いもあります: *MizEye* に関係すると分かっているものは何で、
どの記録を通じてか。`traverse(entity, max_hops, limit)` は entity の近傍 — 別名、その上に
宣言された関係をホップ上限まで、それが到達する entity、各々を言及する記録の ref — を、
順序が決定論的で `limit` に有界なグラフとして返します。グラフそのものの照会ツールです。
記録のテキストは返しません; ref は他と同じく `get_contents` で展開します。

## 5. 不変条件 { #5-invariants }

1. **`recall` は変わらない。** この設計のいかなるものも `recall` の経路では読まれない。
   テスト: recall のテストスイートが、新テーブルにデータの入ったデータベースに対して無改変で通る。
2. **空のグラフは no-op。** entity・言及・関係が無ければ、`reconstruct` はテーブルの無い同じ
   呼び出しと byte-identical な出力を返す。テスト: fixture コーパスを schema v15 あり/なしで
   再構成して等しいこと; グラフを無条件に参照する変異がこれを赤くしなければならない。
3. **連想は item を作らず、並べ替えない。** グラフありの item の head と順序は、なしの時と
   等しい; グラフは item の内側に根拠を足し、宣言された record 関係で束ね、roles を付けるだけ。
   テスト: head の ref と順序を assert する; 到達した記録が item になる変異がこれを赤くしなければ
   ならない。
4. **モデルを呼ばない。** 宣言は書かれたまま保存され、walk は SQL と純関数。
5. **有界で決定論的。** `max_hops` を超えて辿らず、`max_evidence` を超えて残さない; 切り詰めは
   §3 の書かれた順序に従い、同じデータベース・問い・bounds は同じ出力を与える。
6. **すべての要素が出自を言う。** 関係は誰が宣言したかと、あれば根拠の記録を記録する; walk が
   足した記録はどの述語で何ホップかを言う。
7. **分離は保たれる。** walk は `agent_id` を越えない; `project_id` と `channel` は呼び出しの
   読み取り意味論に従い、呼び出しが直接読めないスコープへ関係を辿ることはない。
8. **宣言は端点より長生きしない。** entity・memory・episode の削除は、それに依存するすべての
   別名・言及・関係を取り除く。

## 6. 想起プロセスとの関係、`recall` との関係 { #6-relationship-to-the-recall-process-and-to-recall }

[§1](RELIABLE_RECALL_2_6.md#1-deliberative-recall-the-recall-process) の想起プロセスは後の
ラインです。それが入ると、宣言された関係は cue になります: 登録 entity へのヒットがその上に
宣言された関係を名指し、次の反復は索引から二度目の取得をせずにそこを見ます。それは §3 の walk を
呼び出しごとに 1 回でなく反復ごとに 1 回走らせることで、§3 が walk をプールとスコープ内の
関係の純関数に保っている理由です。この設計はループを作りません; ループが辿るエッジを作ります。

`recall` は平らなままです。以前の決定では walk は gate の後ろで `recall` の中にあり、直接
ヒットの下に関連行を足していました。これは保留にします (§7)。理由は 3 つ: `recall` は gate も
A/B も無しに byte 安定な契約を保つ; 順位リストに足された関連行はまさに以前 fuzzy 拡張が退行した
場所であり、item 内の根拠は有界で、ラベル付きで、決して並べ替えない; walk の呼び出し点が 1 つなら、
recall 側の拡張と reconstruct 側の拡張の間でホップ予算が複利になりえない。

## 7. 別途決めること { #7-decided-separately }

- **`recall` の中の連想** — グラフ経由で到達した行を直接ヒットの下に並べ、default-off の
  gate の後ろに置き、contamination ベンチマークの A/B で admit する。想起プロセスが計測される
  まで保留; その時にはそちらに吸収されるかもしれない。
- **結果としての entity** — 引用ではなく entity *である* reconstruct item。この段階では
  入れない; `traverse` が entity 中心の照会。
- **entity の埋め込み** (2.6.1) — 登録語ごとの安定ベクトル。名前から言い回しが drift した
  記録でも届くように。同じ機能で、新しいツールは無し。
- **推論された関係** — この宣言集合を grounding に、confidence 付きで関係を提案する層。
  別のテーブル、別の信頼水準; ここに混ぜない。
- **anchor が削除された関係を残す** (anchor を空にして)。この段階は削除する — すべての
  anchor が解決するように; 代替案は主張を残して根拠を失う。

## 8. この段階の判定 { #8-how-the-step-is-judged }

この段階が名乗る価値は能力です: 無かった決定論的経路が存在するようになる。ベンチマークの
利得は名乗らず、既定では与えられもしません — ベンチマークのコーパスには宣言が無く、無ければ
不変条件 2 により出力は byte-identical です。利得は何かが宣言して初めて測れ、その計測は
走らせる前に登録します:

- **oracle 腕**: ベンチマーク自身の注釈 (evidence session、entity ラベル) から機械的に導いた
  関係。サーバ側の walk が寄与しうる上界を与える。
- **抽出器の腕**: サーバの外のハーネスで抽出段が宣言した関係。実配備が得るもの — 抽出の質と
  walk の積 — を測る。
- **対照**: 宣言なしの同じ質問。変更前の出力と等しくなければならない。

前もって述べる予想の形: 利得があるなら multi-hop と entity 中心の質問型に集中する; single-hop の
質問は不変で、そこの変化は利得ではなく束ねの退行と数える。上がった item は 1 つずつ、到達した
根拠が問いに答えたのか、ただ取り囲んだだけかを検分します。

その計測より前に、この段階は不変条件 1–8 で出荷します。各々にテストと、それを赤くする名指しの
変異が付きます。

## 9. 最初の実装が確定させた読み方 { #9-readings-the-first-implementation-fixed }

§3 は 6 点を実装に委ねていました。それぞれ次のように決め、各々にテストと、それを赤くする
変異が付いています。

- **向き。** walk は entity → entity の関係をどちらの端からも辿ります。連想は到達です:
  *Kirari maintains MizEye* は、問いがどちらを名指しても 2 つを結びます。述語は宣言された
  とおりに報告します。
- **ホップは 1 から。** 記録が根拠になるのは、少なくとも 1 本の宣言関係を通った時だけです。
  候補と同じ entity を言及する記録は根拠になりません: entity の共有は関係ではなく、束ねキーでも
  ないのと同じです。
- **1 行は 1 箇所。** 既に候補プールにある記録は、検索が置いた場所に留まります。2 つの item が
  到達する記録は前の item に属します。walk は件数の窓に入った item に対し item 順で走るので、
  ある item に足されるものは `count` に依存しません。
- **読み取りも有界。** 到達した entity ごとに、読む記録は最大 `max_evidence` 件、id の小さい順
  — 1 本の関係の内側で切り詰めが既に使っている順序 — です。それより多い entity があれば、
  応答は `bounds.reached` に `max_evidence` を挙げます: 上限に達しており、その先に何があるかは
  分かりません。
- **`why` と独立性。** 宣言関係が束ねた行は `why: "relation:<predicate>"` を持ち、`hops` を
  持ちません; walk が到達した行は同じものに `hops` を持ちます。関係が作ったクラスタの
  `independence_reason` は `"cluster:relation"` です。
- **段 1 が足すのは票であって、通過ではない。** 追加語は、問いの語では得られない字面の一致を
  記録に与えます。融合された行はそれでも recall の品質ゲートを通る必要があり、ゲートは
  変わらない問いに対して採点するので、字面のアームだけが見つけた記録は、別名が無い時と
  まったく同じくゲートの下に留まります。ゲートは recall のもので、この段階は変えません。
  宣言された名前での一致をそれだけで通すかどうかは、別の判断です。
