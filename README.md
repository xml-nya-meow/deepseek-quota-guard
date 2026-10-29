# DeepSeek 额度管理

Windows 本地额度管理工具，当前为**候选预发布版**。它按人员绑定平台 Key，核对累计费用，并在达到个人额度或公共人均额度中的任一上限时触发自动撤销流程。

**真实平台上的自动删除尚未完成验证。请勿把本版本当作已经验证的生产止损工具。** 删除 Key 不可恢复；本程序无法拦截已经发出的请求，也不能保证零超支。

## 工作方式

- 人员表只填写平台 Key 名和个人额度，不填写真实 API Key。公共人均额度固定为本期总预算除以人数后向下取整。
- 费用、账户连续性、人员名和 Tracking ID 核验通过后，达到个人额度或公共人均额度都会触发撤销。不能核实的数据保留为未知或历史已知下界。
- 平台账单可能延迟；页面改版、身份变化、日期范围或字段校验失败时停止相关操作。结果未知的删除不会自动重试，需要人工核实。
- 登录使用独立浏览器上下文，不读取日常浏览器的 Cookie；关闭程序后不保留登录状态。
- 人员名、Tracking ID、费用及处理记录保存在本机数据目录中。不要把数据目录、日志、账单或登录截图提交到仓库或公开 issue。

完整操作说明见 [使用说明](使用说明.md)。官方 CSV/ZIP 导出目前仅用于检查结构和日期，不能据此自动推算逐 Key 的实际扣费。

## 从源码运行

需要 Windows x64 和 Python 3.12.14。以下命令均在仓库根目录执行，使用 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD 'browser-runtime'
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe main.py --data-dir .\data
```

先用 `py -3.12 --version` 确认补丁版本为 3.12.14。安装依赖和浏览器会联网下载软件；首次运行应用不会自动调用付费模型接口。平台登录和费用读取需要网络。

`requirements.txt` 列出直接依赖，`requirements.lock` 固定当前 Windows 构建环境中的直接与传递依赖。请使用干净虚拟环境，更新依赖时同时更新锁文件和第三方声明。

## 运行测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试使用合成数据和临时目录，不需要平台账户或真实 Key，不发送付费请求。GUI 流程测试需要 Windows 桌面环境。测试通过不能替代真实账单对账与自动删除验收。

## 构建便携目录

```powershell
.\.venv\Scripts\python.exe build.py --install-browser --output .\dist\DeepSeekQuotaManager-win-x64
```

如果已经准备好匹配当前 Playwright 版本的浏览器目录，可省略下载：

```powershell
.\.venv\Scripts\python.exe build.py --runtime .\browser-runtime --output .\dist\DeepSeekQuotaManager-win-x64
```

构建脚本校验 Python 和锁定依赖，从已安装 Playwright 的清单确定 Chromium、FFmpeg 和 Windows 辅助组件版本，在独立临时目录中打包，然后移入指定输出位置。**输出路径必须尚不存在；脚本不会覆盖已有便携目录或用户数据。** 重新构建时请选择新的输出路径。

产物是整个便携文件夹，包含 `额度管理.exe`、`_internal`、第三方许可、说明文档和不含本机路径的 `BUILD-INFO.json`。分享时必须复制整个文件夹。源码、运行数据和浏览器缓存不作为整体递归打包；构建仅纳入指定入口、依赖和必要浏览器组件。

固定依赖便于重复构建，但当前不承诺不同机器产出的二进制逐字节一致。发布前仍须在没有 Python 开发环境的全新 Windows x64 环境中验证。

## CI 与便携检查

推送和 pull request 会运行 Windows 单元测试。通过 GitHub Actions 的手动运行入口勾选 `build_portable` 后，额外下载 Chromium、构建便携目录，并执行界面启动及 `about:blank` 浏览器检查。检查使用独立临时数据目录，不登录 DeepSeek、不读取真实账单、不调用付费接口。

手动构建生成 ZIP 及 SHA-256 校验文件，作为 workflow artifact 保存；流程不会自动创建 Release。浏览器启动检查仅验证打包完整性，不能证明网页适配或真实撤销成功。

## 当前边界

- 真实非零费用对账与真实自动删除仍需在隔离、可控的场景中验证。
- 平台费用适配使用网页响应格式，并非承诺稳定的公开管理 API。
- 只接受覆盖完整预算周期的数据；尚未实现跨多段日期自动汇总。
- 费用延迟、在途请求和并发调用可能导致超额。
- 自动操作结果未知时需要人工核实，不应靠重复删除判断是否成功。
- 自动撤销须有另一存活 Key 核验账户连续性；最后一个 Key 需手动处理。重启核实须有存活的已绑定 Key。

本项目与 DeepSeek 没有官方隶属关系。组件版本与来源见 [第三方声明](THIRD-PARTY-NOTICES.md)。

项目源码采用 [MIT 许可证](LICENSE)，版权归 xml-nya-meow。附带组件分别遵循各自许可证。
