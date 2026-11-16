# 第三方组件：Frida

本目录用于存放 Frida 官方发布的独立命令行工具 `frida-inject`（macOS arm64 / x86_64），
仅用于「抓密钥」流程：在一份**临时签名的微信副本**启动前挂上 `CCKeyDerivationPBKDF`
监听。不修改正式版微信、不关闭 SIP。

- 项目：Frida — https://frida.re ，源码 https://github.com/frida/frida
- 版本：17.25.2（固定版本）
- 来源：官方 GitHub Release 资产 `frida-inject-17.25.2-macos-{arm64,x86_64}.xz`
- 校验：见 `SHA256SUMS`（.xz 与官方 Release 页面给出的 sha256 一致；解压后的二进制另列 sha256）
- 许可证：wxWindows Library Licence 3.1（LGPL 2.0+ 外加允许以目标码形式自由分发的例外条款），全文见 `LICENSE-frida.txt`
- 修改：未修改。二进制保持官方签名原样分发。

二进制文件体积较大（约 85 MB / 55 MB），**不提交到 git**。
- 安装包（.dmg）构建时由 `app/build.sh` 调用 `fetch.py` 下载并校验后内置；
- 从源码使用时运行 `python3 vendor/frida/fetch.py` 即可（只连 GitHub，不经过 pip/PyPI）。
