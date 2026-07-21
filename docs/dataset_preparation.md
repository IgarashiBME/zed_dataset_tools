# 抽出画像の選別とデータセット構築

## 目的

`dataset_prepare.py`は、`svo_extract.py`が生成した抽出manifestを集約し、画像レビュー、セッションごとの選択、site・増分別のファイル配置、累積データセットYAMLの生成を行う。

元のSVO2、抽出画像、抽出manifestは変更しない。レビュー作業領域と完成データセットは`ridge_data/`の下で分離する。

## 出力構成

設定例の完成データセット：

```text
../ridge_data/
├── .review/
│   └── dataset01_20260611_ehime/
│       ├── candidates.csv
│       ├── review.csv
│       ├── selection.csv
│       ├── site_map.csv
│       ├── review_config.yaml
│       └── thumbnails/
│
└── dataset01_20260611_ehime/
    ├── images/
    │   ├── site01_add010/
    │   ├── site01_add030/
    │   ├── site01_add060/
    │   ├── site01_add100/
    │   ├── site02_add010/
    │   └── ...
    ├── right/
    │   └── <imagesと同じsite・増分構成>/
    ├── depth/
    │   └── <imagesと同じsite・増分構成>/
    ├── depth_preview/
    │   └── <imagesと同じsite・増分構成>/
    ├── labels/
    │   └── <imagesと同じsite・増分構成>/
    ├── yaml/
    │   ├── dataset_n010.yaml
    │   ├── dataset_n040.yaml
    │   ├── dataset_n100.yaml
    │   └── dataset_n200.yaml
    └── metadata/
        ├── manifest.csv
        ├── selection.csv
        ├── site_map.csv
        └── dataset.yaml
```

`images/`はleft画像である。right、depth、depth preview、labelsは同じディレクトリ名と`image_id`で対応する。

## siteと増分

1つの抽出セッションを1つのsiteとして扱う。初回`plan`時に、セッションIDの昇順で`site01`、`site02`、...を割り当て、`site_map.csv`へ固定する。

```csv
site_id,session_id
site01,20260611_093705
site02,20260611_102930
```

`plan --overwrite`で候補を更新しても既存セッションのsite番号は維持する。新しいセッションには未使用の次番号を割り当てる。

各siteの増分は互いに重複しない。

- `add010`: 新規10枚
- `add030`: 新規30枚
- `add060`: 新規60枚
- `add100`: 新規100枚

累積データセットは画像を複製せず、YAMLの参照先を増やして表現する。

- `dataset_n010.yaml`: 全siteの`add010`
- `dataset_n040.yaml`: 全siteの`add010 + add030`
- `dataset_n100.yaml`: 全siteの`add010 + add030 + add060`
- `dataset_n200.yaml`: 全siteの全増分

33 siteなら`dataset_n200.yaml`が参照する画像は合計6,600枚になる。

## 設定

`configs/dataset.example.yaml`の主な項目：

```yaml
input:
  roots:
    - ../20260611-12Ehime_images
    - ../20260611-12Ehime_images_outer
  manifest_pattern: "*_train/*/manifest.csv"

output:
  root: ../ridge_data
  dataset_name: dataset01_20260611_ehime
  review_dir: .review

selection:
  scope: per_session
  increments: [10, 30, 60, 100]
  seed: 42

review:
  import_from: ../20260611-12Ehime_datasets/review
```

別データセットを作る場合は`output.dataset_name`を変更する。

## 実行方法

すべて`zed_dataset_tools`ディレクトリから実行する。

### 1. レビュー計画

```bash
python3 scripts/dataset_prepare.py plan \
  --config configs/dataset.example.yaml
```

正常に抽出済みで、設定した必須モダリティが存在する画像だけを候補にする。同一`image_id`を重複排除し、seed付きでセッションを交互に並べる。画像はまだコピーしない。

抽出manifestを追加した後に更新する場合：

```bash
python3 scripts/dataset_prepare.py plan \
  --config configs/dataset.example.yaml \
  --overwrite
```

既存`image_id`のレビュー結果とsite割当は保持する。設定例では初回plan時に旧`20260611-12Ehime_datasets/review/review.csv`も読み込み、Keep/Reject/Holdを新しい作業領域へ引き継ぐ。元のCSVは変更しない。

### 2. レビュー

```bash
python3 scripts/dataset_prepare.py review \
  --config configs/dataset.example.yaml
```

ブラウザで`http://127.0.0.1:8765`を開く。

既定のフォーカスモードでは、left画像を左側に1枚だけ大きく表示し、右サイドバーに表示切替、Reject理由、メモ、Keep/Reject/Holdを常時表示する。保存成功後に次の未レビュー画像へ進む。Reject理由の既定値は`other`である。

キーボード操作：

- `K`: Keep
- `R`: Reject
- `H`: Hold
- `L`: left
- `V`: right
- `D`: depth preview
- `←` / `→`: 前後へ移動

Sessionフィルターには`site ID — session ID`とKeep数を表示する。最後は未達siteへ絞り込み、各siteのKeepを200枚以上にする。

### 3. 進捗確認

```bash
python3 scripts/dataset_prepare.py status \
  --config configs/dataset.example.yaml
```

200枚未満のsiteについて、site ID、session ID、残り枚数を表示する。

### 4. 増分割当

```bash
python3 scripts/dataset_prepare.py select \
  --config configs/dataset.example.yaml
```

すべてのsiteでKeepが200枚以上必要である。各site内でKeep画像をseed付きで再度並べ替え、10、30、60、100枚の増分へ割り当てる。

レビュー変更後に再選択する場合：

```bash
python3 scripts/dataset_prepare.py select \
  --config configs/dataset.example.yaml \
  --overwrite
```

### 5. データセット構築

```bash
python3 scripts/dataset_prepare.py build \
  --config configs/dataset.example.yaml
```

設定例では`../ridge_data/dataset01_20260611_ehime/`を作る。既存データセットは上書きしない。作り直す場合は`output.dataset_name`を変更する。

`materialize.mode`は`copy`または`hardlink`を指定できる。配布・アノテーション用に元画像から独立させる場合は`copy`を使用する。

### 6. 検証

```bash
python3 scripts/dataset_prepare.py verify \
  ../ridge_data/dataset01_20260611_ehime
```

ファイルの存在、site・増分別の件数、モダリティ間の配置、ラベル状態、4つの累積YAMLの参照先、metadataを検査する。

## ラベル

TXTラベルの例：

```yaml
labels:
  format: yolo_segmentation
  extension: txt
  source_root: ../annotations/nakaaze
  required: false
  target_view: left
```

`source_root/<image_id>.txt`が存在すれば対応する`labels/siteXX_addNNN/`へ取り込む。空のTXTも「確認済みで対象なし」の有効なラベルとして扱う。ラベルがなければディレクトリだけを作り、manifestを`unlabeled`とする。

生成YAMLの`train`には`images/siteXX_addNNN`のリストを記録する。validation/testは学習に使っていない別撮影データを指定するため、初期値は未設定である。

## combined YAML

本プログラムは1回につき`output.dataset_name`で指定した1データセットだけを構築する。複数データセットを参照する`ridge_data/combined_yaml/`は現時点では生成しない。

## 安全性

- 元画像と抽出manifestは変更しない。
- reject画像は削除しない。
- 候補順、site内選択、増分割当はseedで再現する。
- 完成データセットを上書きしない。
- testデータは本ツールの学習用選択に混ぜない。
