# Memory1 全流程逐步对拍（88 步）

- case: `memory1-full-dx10` · probe 5256 点 · digest `5091b879f0d71ae9`
- 域: 0.624 × 0.74 × 2.481 µm，Δx = 10 nm
- 引擎: viennaps 4.7.0 / spx V-2024.03
- 耗时: viennaps 439 s / spx 1088 s
- 结果: 88/88 步两个引擎均 ok

| # | step | 一致率 | 最低 IoU 材料 |
|---|---|---|---|
| 0 | `s0_substrate` | 1.0 | Si 1.0 |
| 1 | `01_depo_fill_Oxide2_50nm` | 1.0 | Oxide2 1.0 |
| 2 | `02_depo_fill_Nitride_35nm` | 1.0 | Oxide2 1.0 |
| 3 | `03_depo_fill_Oxide_28nm` | 1.0 | Oxide2 1.0 |
| 4 | `04_depo_fill_Oxide_50nm` | 0.9726 | SiO2 0.3333 |
| 5 | `05_depo_fill_SiOC_100nm` | 0.9589 | SiOC 0.25 |
| 6 | `06_depo_fill_BARC_50nm` | 0.9315 | BARC 0.0 |
| 7 | `07_plug_mask` | 0.86 | BARC 0.0 |
| 8 | `08_etch_aniso_BARC_SiOC` | 0.86 | BARC 0.0 |
| 9 | `09_etch_strip_resist_BARC` | 0.9589 | SiOC 0.25 |
| 10 | `10_etch_aniso_ON` | 0.9589 | SiOC 0.25 |
| 11 | `11_etch_strip_SiOC` | 0.9726 | SiO2 0.3333 |
| 12 | `12_depo_iso_SiO_5nm` | 0.9863 | SiO2 0.6667 |
| 13 | `13_depo_iso_SiN_5nm` | 0.9726 | Si3N4 0.0 |
| 14 | `14_depo_iso_SiON_7nm` | 0.9726 | Si3N4 0.0 |
| 15 | `15_depo_iso_poly` | 0.9726 | PolySi 0.0 |
| 16 | `16_depo_iso_OxideCVD` | 0.9315 | PolySi 0.0 |
| 17 | `17_etch_cmp` | 0.9726 | SiO2 0.3333 |
| 18 | `18_depo_fill_CESL_3nm` | 0.9726 | SiO2 0.3333 |
| 19 | `19_depo_fill_Oxide_60nm` | 0.9452 | SiO2 0.2 |
| 20 | `20_depo_fill_SiOC_100nm` | 0.8904 | SiOC 0.0 |
| 21 | `21_depo_fill_BARC_50nm` | 0.8493 | BARC 0.0 |
| 22 | `22_slit_mask` | 0.8174 | BARC 0.0 |
| 23 | `23_etch_aniso_BARC_SiOC` | 0.8204 | BARC 0.0 |
| 24 | `24_etch_strip_resist_BARC` | 0.9026 | SiOC 0.0 |
| 25 | `25_etch_ellipso_oxide_taper` | 0.9026 | SiOC 0.0 |
| 26 | `26_etch_aniso_ON_CESL` | 0.8965 | SiOC 0.0 |
| 27 | `27_etch_strip_SiOC` | 0.9513 | SiO2 0.1892 |
| 28 | `28_etch_strip_Nitride` | 0.9406 | SiO2 0.1522 |
| 29 | `29_depo_iso_AlO_3nm` | 0.9376 | Al2O3 0.0 |
| 30 | `30_depo_iso_TiN_2nm` | 0.9376 | Al2O3 0.0 |
| 31 | `31_depo_iso_W_15nm` | 0.9346 | Al2O3 0.0 |
| 32 | `32_etch_aniso_TiN_W` | 0.9376 | Al2O3 0.0 |
| 33 | `33_etch_iso_AlO_TiN_W` | 0.9376 | Al2O3 0.0 |
| 34 | `34_depo_iso_oxide` | 0.9391 | TiN 0.0 |
| 35 | `35_depo_iso_W` | 0.7979 | Al2O3 0.0 |
| 36 | `36_etch_cmp` | 0.8813 | Al2O3 0.0 |
| 37 | `37_depo_fill_oxide_150nm` | 0.7854 | Al2O3 0.0 |
| 38 | `38_depo_fill_SiOC_100nm` | 0.7443 | Al2O3 0.0 |
| 39 | `39_depo_fill_BARC_50nm` | 0.7169 | Al2O3 0.0 |
| 40 | `40_plug_mask` | 0.6484 | Al2O3 0.0 |
| 41 | `41_etch_aniso_BARC_SiOC` | 0.6484 | Al2O3 0.0 |
| 42 | `42_etch_strip_resist_BARC` | 0.7443 | Al2O3 0.0 |
| 43 | `43_etch_aniso_oxide` | 0.7443 | Al2O3 0.0 |
| 44 | `44_etch_aniso_CESL` | 0.7443 | Al2O3 0.0 |
| 45 | `45_etch_strip_SiOC` | 0.7854 | Al2O3 0.0 |
| 46 | `46_depo_iso_TiN` | 0.7854 | Al2O3 0.0 |
| 47 | `47_depo_iso_W` | 0.715 | Al2O3 0.0 |
| 48 | `48_etch_cmp` | 0.7549 | Al2O3 0.0 |
| 49 | `49_depo_fill_CESL_3nm` | 0.7549 | Al2O3 0.0 |
| 50 | `50_depo_fill_oxide_100nm` | 0.7139 | Al2O3 0.0 |
| 51 | `51_depo_fill_SiOC_100nm` | 0.7139 | Al2O3 0.0 |
| 52 | `52_depo_fill_BARC_50nm` | 0.6865 | Al2O3 0.0 |
| 53 | `53_bitline_strap_mask` | 0.618 | Al2O3 0.0 |
| 54 | `54_etch_aniso_BARC_SiOC` | 0.618 | Al2O3 0.0 |
| 55 | `55_etch_strip_resist_BARC` | 0.7139 | Al2O3 0.0 |
| 56 | `56_etch_aniso_oxide` | 0.7139 | Al2O3 0.0 |
| 57 | `57_etch_aniso_CESL` | 0.7139 | Al2O3 0.0 |
| 58 | `58_etch_strip_SiOC` | 0.7139 | Al2O3 0.0 |
| 59 | `59_depo_iso_TiN` | 0.7139 | Al2O3 0.0 |
| 60 | `60_depo_iso_W` | 0.7139 | Al2O3 0.0 |
| 61 | `61_etch_cmp` | 0.7135 | Al2O3 0.0 |
| 62 | `62_depo_fill_CESL_3nm` | 0.7135 | Al2O3 0.0 |
| 63 | `63_depo_fill_oxide_75nm` | 0.6998 | Al2O3 0.0 |
| 64 | `64_depo_fill_SiOC_100nm` | 0.6587 | Al2O3 0.0 |
| 65 | `65_depo_fill_BARC_50nm` | 0.6039 | Al2O3 0.0 |
| 66 | `66_bitline_contact_mask` | 0.5765 | Al2O3 0.0 |
| 67 | `67_etch_aniso_BARC_SiOC` | 0.5765 | Al2O3 0.0 |
| 68 | `68_etch_strip_resist_BARC` | 0.6587 | Al2O3 0.0 |
| 69 | `69_etch_aniso_oxide` | 0.6587 | Al2O3 0.0 |
| 70 | `70_etch_aniso_CESL` | 0.6587 | Al2O3 0.0 |
| 71 | `71_etch_strip_SiOC` | 0.6998 | Al2O3 0.0 |
| 72 | `72_depo_iso_TiN` | 0.6865 | Al2O3 0.0 |
| 73 | `73_depo_iso_W` | 0.6849 | Al2O3 0.0 |
| 74 | `74_etch_cmp` | 0.6986 | Al2O3 0.0 |
| 75 | `75_depo_fill_CESL_3nm` | 0.6849 | Al2O3 0.0 |
| 76 | `76_depo_fill_oxide_50nm` | 0.6849 | Al2O3 0.0 |
| 77 | `77_depo_fill_SiOC_100nm` | 0.6575 | Al2O3 0.0 |
| 78 | `78_depo_fill_BARC_50nm` | 0.6027 | Al2O3 0.0 |
| 79 | `79_bitline_mask` | 0.5753 | Al2O3 0.0 |
| 80 | `80_etch_aniso_BARC_SiOC` | 0.5753 | Al2O3 0.0 |
| 81 | `81_etch_strip_resist_BARC` | 0.6575 | Al2O3 0.0 |
| 82 | `82_etch_aniso_oxide` | 0.6575 | Al2O3 0.0 |
| 83 | `83_etch_aniso_CESL` | 0.6575 | Al2O3 0.0 |
| 84 | `84_etch_strip_SiOC` | 0.6849 | Al2O3 0.0 |
| 85 | `85_depo_iso_TaN` | 0.6849 | Al2O3 0.0 |
| 86 | `86_depo_iso_Cu` | 0.6301 | Al2O3 0.0 |
| 87 | `87_etch_cmp` | 0.6849 | Al2O3 0.0 |

中位数 0.744，下四分位 0.685，最低 0.575，1.00 的步数 4。
