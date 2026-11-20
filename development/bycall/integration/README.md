# 数字孪生显示适配层

当前契约、职责、动画边界和部署方式见 ../docs/19-护士站文字与3D双屏同源设计.md。

- twin-location-state.mjs：消费主机全量区域位置快照，生成渲染器无关的人物/锚点状态；不发网络请求、不含密钥，不生成实测轨迹。
- twin-location-state.test.mjs：Node内置测试，运行 `node --test integration/twin-location-state.test.mjs`。

3D项目应由自己的服务端代理GET /integration/twin/locations，使用独立只读密钥；浏览器只消费同源代理响应。不要在前端配置主机管理员密钥。实际场景、地址、模型锚点与显示器健康上报尚未在此工程实施。
