# 抽出画像の選別とデータセット構築

## 目的

`dataset_prepare.py`は、`svo_extract.py`が生成した抽出manifestを集約し、画像の人手レビュー、再現可能な選択、サイズ別subsetの作成、完成データセットの構築を行う。

元のSVO2、抽出画像、抽出manifestは変更しない。レビュー結果と完成データセットは、設定した専用出力ディレクトリへ保存する。

## 出力構成

設定例では次の場所へ出力する。

```text
../20260611-12Ehime_datasets/nakaaze/
├── review/
│   ├── candidates.csv
│   ├── review.csv
│   ├── review_config.yaml
│   ├── thumbnails/
│   └── selection.csv
└── v1/
    ├── left/
    ├── right/
    ├── depth/
    ├── depth_preview/
    ├── labels/
    ├── subsets/
    │   ├── add_0010.txt
    │   ├── add_0030.txt
    │   ├── add_0060.txt
    │   ├── add_0100.txt
    │   ├── dataset_0010.txt
    │   ├── dataset_0040.txt
    │   ├── dataset_0100.txt
    │   ├── dataset_0200.txt
    │   └── by_session/
    │       └── <セッションID>/
    │           ├── add_0010.txt
    │           ├── add_0030.txt
    │           ├── add_0060.txt
    │           ├── add_0100.txt
    │           ├── dataset_0010.txt
    │           ├── dataset_0040.txt
    │           ├── dataset_0100.txt
    │           └── dataset_0200.txt
    ├── manifest.csv
    └── dataset.yaml
```

枚数はすべてセッションごとの指定である。`add_*.txt`は各セッションから同じ枚数を集めた、互いに重複しない全体増分である。`dataset_*.txt`はその累積リストである。`by_session/`には同じ割り当てをセッション別に保存する。

33セッションの場合、`dataset_0200.txt`は各セッション200枚、合計6,600枚を含む。割り当ての正本は`manifest.csv`の`session_id`と`increment_group`列とし、すべてのリストとの一致を`verify`で検査する。

## 基本操作

以下のコマンドは`zed_dataset_tools`ディレクトリから実行する。

### 1. レビュー計画を作る

```bash
python3 scripts/dataset_prepare.py plan \
  --config configs/dataset.example.yaml
```

正常に抽出済みで、設定したleft、right、depth、depth previewが存在する画像だけを候補にする。同一`image_id`は重複排除する。

候補はseed付きでセッションごとにシャッフルし、セッションを交互に並べる。これにより候補順を再現可能にし、特定セッションへの集中を抑える。全候補の順序を`candidates.csv`へ保存するが、すべてをレビューする必要はない。

抽出manifestが増えた後に候補一覧を更新する場合：

```bash
python3 scripts/dataset_prepare.py plan \
  --config configs/dataset.example.yaml \
  --overwrite
```

既存候補と同じ`image_id`のレビュー結果は保持される。選択結果を作成済みの場合は、更新後に`select --overwrite`で作り直す。

### 2. レビュー画面を起動する

```bash
python3 scripts/dataset_prepare.py review \
  --config configs/dataset.example.yaml
```

ブラウザで次を開く。

```text
http://127.0.0.1:8765
```

既定ではフォーカスモードで、未レビュー画像を左側に1枚だけ大きく表示する。右サイドバーにはleft/right/depth preview切替、Reject理由、メモ、Keep/Reject/Holdボタンを常時表示する。判定の保存に成功すると、次の未レビュー画像へ自動的に切り替わる。Reject理由の既定値は`other`である。

フォーカスモードでは次のキーを使用できる。

- `K`: Keepして次へ
- `R`: Rejectして次へ
- `H`: Holdして次へ
- `L`: leftを表示
- `V`: rightを表示
- `D`: depth previewを表示
- `←` / `→`: 前後の候補へ移動

画面上部のボタンでグリッドモードへ切り替えられる。グリッドモードでは複数画像の比較、過去の判定確認、修正を行える。表示用サムネイルは初回アクセス時に`review/thumbnails/`へ遅延生成し、画像をクリックするとleft、right、depth previewをまとめて拡大表示する。

両モードで各セッションの達成状況を確認でき、Sessionフィルターで特定セッションだけをレビューできる。最後は未達セッションへ絞り込み、各セッションのKeepを200枚以上にする。

判定は次の3種類とする。

- `keep`: 採用候補
- `reject`: 不採用。除外理由の既定値は`other`
- `hold`: 保留。採用数には含めない

判定は操作ごとに`review.csv`へ保存する。Reject理由を変更せず判定した場合は`other`として保存する。画像は移動も削除もしない。

進捗だけを確認する場合：

```bash
python3 scripts/dataset_prepare.py status \
  --config configs/dataset.example.yaml
```

### 3. 採用画像を割り当てる

```bash
python3 scripts/dataset_prepare.py select \
  --config configs/dataset.example.yaml
```

設定例では、すべてのセッションにそれぞれ200枚以上のkeepが必要になる。各セッション内のkeep画像をseed付きで再度並べ替え、セッションごとに10、30、60、100枚の増分へ割り当てて`selection.csv`へ保存する。

33セッションなら選択結果は合計6,600枚になる。1つでもkeepが200枚に達していないセッションがあれば、`select`は不足セッションを表示して停止する。

レビュー判定を変更した後に選択結果を更新する場合：

```bash
python3 scripts/dataset_prepare.py select \
  --config configs/dataset.example.yaml \
  --overwrite
```

### 4. データセットを構築する

```bash
python3 scripts/dataset_prepare.py build \
  --config configs/dataset.example.yaml
```

同一ファイルシステムでは既定でハードリンクを使用する。ハードリンクを作成できない場合はコピーする。配布用に独立したファイルが必要なら`materialize.mode: copy`を指定する。

既存バージョンは上書きしない。内容を変更する場合は`output.version`を`v2`などへ変更する。

### 5. 完成データセットを検証する

```bash
python3 scripts/dataset_prepare.py verify \
  ../20260611-12Ehime_datasets/nakaaze/v1
```

manifestの重複、各モダリティ、ラベル状態、増分間の重複、累積subsetとの一致を検査する。

## ラベル

ラベル拡張子は固定していない。TXTラベルの例：

```yaml
labels:
  format: yolo_segmentation
  extension: txt
  source_root: ../annotations/nakaaze
  required: false
  target_view: left
```

`source_root/<image_id>.txt`が存在すれば`labels/`へ取り込み、manifestを`completed`とする。空のTXTも「確認済みで対象なし」の有効なラベルとして扱う。ファイルがなければ`unlabeled`とする。`required: true`の場合は、選択画像のラベルが1つでも欠けていれば構築を停止する。

PNGマスクを利用する場合は`format`と`extension`を変更できる。

## 再現性と安全性

- 候補提示順と最終割り当てには設定したseedを使用する。
- 元画像と抽出manifestは読み取り専用として扱う。
- reject画像は削除しない。
- 完成データセットはバージョン単位で上書きしない。
- train/validation/testの分割は本ツールでは行わない。テストには別撮影セッションの未使用データを使用する。
