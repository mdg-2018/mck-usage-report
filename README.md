# MongoDB Usage Counter

## Overview
This script aggregates the memory assigned to pods owned by MongoDBCommunity objects in your kubernetes cluster.

## Usage
Flags include:
--namespaces | list of namespaces you want the script to scan
--context | kubernetes context that you want the script to use
--kubeconfig | path to kubeconfig file you want used

```shell
python getCommunityUsage.py --namespaces=[mongodb] --context default --kubeconfig ./kube.yaml
```