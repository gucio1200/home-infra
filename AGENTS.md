# home-infra

GitOps repo for a Talos Linux Kubernetes homelab. Flux reconciles `k8s/` from `main`; everything outside `k8s/` is ignored by the cluster.

## Layout

- `k8s/flux/`: Flux entry point. `config/` installs Flux from the `flux-manifests` OCI artifact with controller patches and defines the `flux-system` GitRepository; `sources/` holds legacy HelmRepositories; `vars/` holds `cluster-settings`; `apps.yaml` is the `apps` Kustomization for `k8s/apps` and patches SOPS decryption into every child Kustomization.
- `k8s/apps/<namespace>/kustomization.yaml`: lists `namespace.yaml` and each `<app>/ks.yaml`.
- `k8s/apps/<namespace>/<app>/ks.yaml`: Flux Kustomization in `flux-system` with `targetNamespace: <namespace>` and `path: ./k8s/apps/<namespace>/<app>/app`.
- `k8s/apps/<namespace>/<app>/app/`: `kustomization.yaml`, `helmrelease.yaml`, `ocirepository.yaml`, `externalsecret.yaml`, extra files in `resources/`.
- `k8s/components/`: Kustomize Components (`volsync`, `dragonfly`, `keda/*`, `mcp-auth`).
- `k8s/bootstrap/`: Talos config for talhelper (`talos/talconfig.yaml`; generated `clusterconfig/` is gitignored) and Flux bootstrap secrets.
- `k8s/tools/`: one-off maintenance manifests and scripts; Flux does not apply them.
- `.github/`: Renovate config (`renovate.json5`, `renovate/*.json5`) and the flux-local CI workflow.

## Conventions

- New app: copy the shape of an existing one (e.g. `k8s/apps/ai/kodi-mcp`), add `./<app>/ks.yaml` to the namespace `kustomization.yaml` and every new file to `app/kustomization.yaml`.
- Charts: HelmRelease `chartRef` to a per-app `OCIRepository`, mostly `oci://ghcr.io/bjw-s-labs/helm/app-template`. Add a HelmRepository to `k8s/flux/sources/` only when a chart has no OCI artifact.
- Images: pin as `tag@sha256:<digest>`; Renovate bumps them. For versions Renovate cannot detect, add a `# renovate: datasource=... depName=...` annotation.
- Secrets: `ExternalSecret` from the `onepassword-connect` ClusterSecretStore. Never commit plain secrets. SOPS (`*.sops.yaml`, age) is only for bootstrap.
- Components: attach through `spec.components` in `ks.yaml` and parametrise with `postBuild.substitute` (`APP`, `VOLSYNC_CLAIM`, `VOLSYNC_CAPACITY`, ...). Never point `spec.path` at a Component directory. Before adding `postBuild.substitute`, check the app's manifests contain no literal `${`.
- Dependencies: `dependsOn` the providers an app needs, e.g. `external-secrets`, `cluster-apps-rook-ceph-cluster`, `cluster-apps-volsync-app`.
- Scheduling: only nodes labelled `feature.node.kubernetes.io/cpu-cpuid.AVX2: "true"` have AVX2; pin images that need it with that node affinity. Some nodes start Python apps about twice as slowly, so give such apps a startupProbe rather than relying on liveness delays.
- New files carry no comments and no `yaml-language-server` modeline; existing files keep theirs. `# renovate:` annotations are fine.

## Validation

- One directory: `kustomize build <dir> --load-restrictor=LoadRestrictionsNone`.
- Whole repo, as CI does: `flux-local test --enable-helm --all-namespaces --path k8s/flux -v`.
- Rendered diff against `main`: `flux-local diff helmrelease --path k8s/flux --path-orig <main checkout>/k8s/flux --all-namespaces --unified 6` (same with `kustomization`).
- Against the live cluster: build the manifests and diff them with the flux MCP `diff_kubernetes_manifest`.

## Cluster access

- `talosctl` works directly. `kubectl` needs a kubeconfig from `talosctl kubeconfig -n <one node>`; always pass `--request-timeout`.
- Live Talos and Kubernetes versions can be newer than `talconfig.yaml`; read them from the cluster before touching `k8s/bootstrap/talos`.

## Workflow

- Branch from a fresh `origin/main` and open a PR with `gh`; the owner merges. Never push to `main`.
- Commit message and PR body: a short title, then `Issue:` and `Solution:` with 1-3 one-line bullets each.
- Merging deploys. Afterwards run `flux reconcile source git flux-system -n flux-system` and `flux reconcile ks <app> -n flux-system`, then check `flux get hr -A`, the pods and Warning events.
- Never `kubectl apply`, `edit` or `patch` resources Flux manages; change git instead.
