# Lexizx｜考研英语学习工具

面向考研英语学习的 Windows 桌面应用。下载后双击即可使用，无需注册。

**[下载最新版 word.exe](https://github.com/1826548016-cloud/Lexizx/releases/latest/download/word.exe)** · [版本与更新说明](https://github.com/1826548016-cloud/Lexizx/releases/latest)

支持词汇背诵与复习、考研真题和写作、英语一长难句翻译、六级汉译英、AI 组卷与学习助手。

AI 功能需要联网并自行配置模型 API，可能产生服务费用；AI 内容仅供学习参考。学习数据保存在本机，换电脑或重装前请先备份。

使用方法、AI 配置、备份恢复和功能说明：[用户使用说明](docs/USER_GUIDE.md)

## 关于本项目

本项目前期由作者手工编写完成框架搭建，后期借助 Codex、Trae 等 AI 编程工具进行功能开发与维护。如果你是技术人员，欢迎阅读源码、了解项目结构后进行二次创作。

<details>
<summary>开发者</summary>

```powershell
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

访问 `http://127.0.0.1:8000/`。打包产物由 `product/` 目录下的脚本生成。

</details>
