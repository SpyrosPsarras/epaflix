import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def render_job():
    job = yaml.safe_load((ROOT / "job.yaml").read_text())
    identities = json.loads((ROOT / "identities.json").read_text())
    job["metadata"]["namespace"] = identities["vaultNamespace"]
    pins = yaml.safe_load((ROOT.parent / "kustomization.yaml").read_text())["images"]
    pin = next(image for image in pins if image["name"] == "alpine/kubectl")
    spec = job["spec"]["template"]["spec"]
    next(container for container in spec["initContainers"] if container["name"] == "kubectl")["image"] = pin["name"] + ":" + pin["newTag"] + "@" + pin["digest"]
    return job


if __name__ == "__main__":
    print(json.dumps(render_job()))
