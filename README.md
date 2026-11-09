# Dobot Xtrainer — 数据采集精简版

基于越疆 Xtrainer 官方程序（源自 GELLO / ACT，见 THIRD-PARTY-LICENSES）裁剪，只保留**主手遥操作 + 三相机录制**，产出 `collect_data/`。训练、推理、HDF5 转换等已移除。

## 目录

```
dobot_control/   主手(Dynamixel)、从臂(Dobot TCP)、夹爪(Feetech)、RealSense 驱动与 ZMQ 节点
  collector.py   采集核心 CollectorSession（按键状态机、控制循环、录制），CLI/TUI/GUI 共用
  settings.py    [COLLECT] [NETWORK] [SAFETY] 运行设置（读写 ini）
  config.py      硬件标定段 [HAND_*] [GRIPPER_*] [CAMERA] 读写
  safety.py      正运动学 + 工作空间/速度/J3 安全检查
  motion.py      回零、插值靠近、关节跳变检查、指示灯
  fake.py        假主手/从臂/相机，无硬件调试用
  calibration.py 找串口、读零位、手柄检查、相机序列号（标定脚本的可复用版本）
  episodes.py    episode 列表、完整性检查、移入回收站
  tui/           Textual 终端界面：采集仪表盘、设置页、episode 管理、标定工具
  format_obs.py  存 pkl；utils.py 串口扫描/授权、mk_dir、wait_period
  robots/controller_info.py  固件版本、机型查询（HTTP 22000）
experiments/     launch_nodes.py（从臂服务）  run_control.py（命令行采集）  run_tui.py（终端界面采集）
scripts/         1_find_port / 2_get_offset / 3_just_buttonA / 5_camera_read
scripts/dobot_config/dobot_settings.ini   串口、标定、相机序列号
third_party/     DynamixelSDK、feetech scservo_sdk（随仓库内置）
```

## 安装

```bash
pip install -r requirements.txt
```

用 `opencv-python`，不要装 `opencv-python-headless`，后者不能弹出相机窗口。

串口权限（只需配置一次，之后程序不再需要 sudo 密码）：

```bash
sudo cp scripts/99-xtrainer-serial.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

规则让 `/dev/ttyUSB*`、`/dev/ttyACM*` 对普通用户可读写，并把 FTDI 串口的 USB 延迟设为 1 ms。没装规则时，程序只会尝试免密码的 `sudo -n`，失败就打印提示，不会询问或保存密码。

## 使用流程（均在仓库根目录执行）

```bash
python -m scripts.1_find_port      # 识别主手/夹爪串口，写入 ini
python scripts/3_just_buttonA.py   # 只测 A 键锁定/同步，用来调整手柄姿态
python -m scripts.2_get_offset     # 主手摆到 start_joints 姿态后标定零位
python -m scripts.5_camera_read    # （可选）检查三相机序列号与方向
python experiments/launch_nodes.py # 终端 1：连接两台从臂，ZMQ 服务
python experiments/run_control.py  # 终端 2：遥操作与录制
python experiments/run_control.py --fake --show-img False  # 无硬件模拟运行
```

也可以用终端界面代替终端 2 的 `run_control.py`：

```bash
python experiments/run_tui.py            # 按 c 连接；--connect 启动即连接
python experiments/run_tui.py --fake     # 无硬件：数字键 1-5 模拟主手按键
```

TUI 显示会话状态、两只手的锁定/同步、录制中的 episode 和帧数、指示灯、控制频率和相机帧率，以及事件日志。键位：`c` 连接/重连，`e` 设置，`l` episode 管理，`k` 标定工具，`v` 打开/关闭相机窗口，`r` 开始/停止录制（同 B 键，也要求至少一只手在同步），`s` 停止会话，`q` 退出（录制中需连按两次）。模拟模式额外有 `1`/`2` 左手 A 短按/长按，`3`/`4` 右手 A 短按/长按，`5` B。相机窗口和原版 `run_control.py` 的弹窗一样（窗口名 `0`，top | left | right 并排），`[COLLECT] show_img = True` 时连接后自动弹出，点窗口的关闭按钮或按 `v` 关闭。窗口需要图形桌面，在没有 `DISPLAY` 的 SSH 终端里会提示不可用，不影响采集。

按键：长按 A 开/关该手同步（黄灯）；短按 A 锁定/解锁；B（任一只手）开始/停止录制（绿灯），仅在至少一只手同步时才能开始。出错（关节跳变、安全保护、相机掉线）时亮红灯并停止会话。

## 输出

```
<仓库上一级>/datasets/<project_name>/collect_data/<YYYYmmddHHMMSS>/
    topImg/ leftImg/ rightImg/  {idx}.jpg   640x480 BGR
    observation/                {idx}.pkl   joint_positions(14) joint_velocities ee_pos_quat gripper_position control(14)
```

每条 episode 的帧号从 1 开始。

## 设置

`scripts/dobot_config/dobot_settings.ini` 中：

| 段 | 内容 |
|---|---|
| `[COLLECT]` | `save_root`（空 = `<仓库上一级>/datasets`）、`project_name`、`show_img`、`jpeg_quality`、`top_crop`（上,下,左,右 比例）、`control_hz`（0 = 不限速） |
| `[NETWORK]` | `left_ip` `right_ip` `zmq_host` `zmq_port` `check_firmware` `robot_type`（auto / Nova 2 / Nova 5） |
| `[SAFETY]` | `enabled`、工作空间范围（mm）、`max_z_down_speed`（m/s）、`servo_step_limit`（rad）、`check_j3` |

缺省值与原脚本写死的值一致。`run_control.py` 的命令行参数可临时覆盖（`--help` 查看）。

也可以在 TUI 里按 `e` 打开设置页编辑这三段：保存（`ctrl+s`）前会检查取值范围，有错的字段标红并在底部列出原因；"Defaults (this tab)" 把当前页恢复为缺省值（保存后才生效）。保存只写这三段，不动标定段；会话进行中保存的设置在下次连接时生效。注意命令行覆盖的值（如 `--project-name`）在设置页里保存后会写入 ini。

## Episode 管理（TUI 按 `l`）

列出当前项目的所有 episode（可在顶部切换项目），显示开始时间、帧数、大小和状态。打开时自动做结构检查：四个子目录都在、图像和 pkl 帧号一致、帧号连续。`v` 对选中的 episode 做深度检查（逐个读取 pkl 并检查字段和形状、检查 jpg 非空）。`d` 把选中的 episode 移到 `<项目>/trash/`，不会真正删除，把目录移回 `collect_data/` 即可恢复；正在录制的 episode 不能移走。

## 标定工具（TUI 按 `k`）

四个标签页对应四个标定脚本，结果先显示，确认后再点保存写入 ini：

| 标签页 | 对应脚本 | 说明 |
|---|---|---|
| Ports | `1_find_port` | 依次探测主手（2M / 1M 波特率）和夹爪，显示 ini 当前值和扫描结果；没找全时保存前会确认，只写找到的 |
| Offsets | `2_get_offset` | 两只主手摆到 start_joints 姿态后读取，显示新旧零位，变化超过 0.5 rad 时提示并在保存前确认 |
| Hand check | `3_just_buttonA` | 短按 A 解锁/锁定，实时显示关节角度和按键，用来调整手柄；离开时两只手都会重新锁定 |
| Cameras | `5_camera_read` | 检测已连接的 RealSense 序列号，分配给 top / left / right；Camera view 按当前分配弹出三相机窗口，用来确认方向和位置 |

会话运行时不能进入标定（会占用主手和相机），先按 `s` 停止。`--fake` 下标定是演示模式，不会写 ini。原来的四个脚本仍然可以单独使用。
