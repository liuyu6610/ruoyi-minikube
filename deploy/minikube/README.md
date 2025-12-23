# RuoYi-Cloud minikube 一键部署

本目录用于把 `RuoYi-Cloud` 一套服务（MySQL/Redis/Nacos + gateway/auth/system/gen/job/file/monitor + nginx）部署到本机 `minikube`。

## 1. 前置条件

- 已安装 ingress-nginx（可选；本方案默认使用 NodePort 暴露 nginx）

## 2. 一键部署

在仓库根目录执行：

```powershell
python .\deploy\minikube\deploy.py
```

常用参数：

```powershell
python .\deploy\minikube\deploy.py --namespace ruoyi
python .\deploy\minikube\deploy.py --skip-build
python .\deploy\minikube\deploy.py --only-apply
python .\deploy\minikube\deploy.py --cleanup
```

说明：

- `--skip-build`：跳过 docker build（直接用你现有镜像）
- `--only-apply`：只做 kubectl apply + 等待就绪
- `--cleanup`：卸载本次部署（删除 namespace）

## 3. 部署完成后的访问方式

本方案把 `ruoyi-nginx` 作为对外入口（前端 + 反向代理到网关 `/prod-api/`）。

获取访问 URL：

```powershell
minikube service -n ruoyi ruoyi-nginx --url
```

然后用浏览器打开输出的 URL。

## 4. 验证与排障

- 查看 pod：

```powershell
kubectl get pod -n ruoyi -o wide
```

- 查看服务：

```powershell
kubectl get svc -n ruoyi
```

- 查看某个服务日志（例如网关）：

```powershell
kubectl logs -n ruoyi deploy/ruoyi-gateway --tail=200
```

- 如果启动慢：

```powershell
kubectl describe pod -n ruoyi <pod-name>
```
