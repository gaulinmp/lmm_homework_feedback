import { 
  BaseCheckpointSaver, 
  WRITES_IDX_MAP, 
  copyCheckpoint, 
  getCheckpointId, 
  maxChannelVersion 
} from "@langchain/langgraph-checkpoint";

const TASKS = "__tasks__";

function _generateKey(threadId, checkpointNamespace, checkpointId) {
	return JSON.stringify([threadId, checkpointNamespace || "", checkpointId]);
}

function _parseKey(key) {
	const [threadId, checkpointNamespace, checkpointId] = JSON.parse(key);
	return { threadId, checkpointNamespace, checkpointId };
}

// Simple Promise wrapper for IndexedDB
class IDBStore {
    constructor(dbName = "LangGraph_Checkpoints", storeNames = ["storage", "writes"]) {
        this.dbPromise = new Promise((resolve, reject) => {
            const request = indexedDB.open(dbName, 1);
            request.onupgradeneeded = (e) => {
                const db = e.target.result;
                storeNames.forEach(name => {
                    if (!db.objectStoreNames.contains(name)) {
                        db.createObjectStore(name);
                    }
                });
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async get(storeName, key) {
        const db = await this.dbPromise;
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readonly");
            const request = tx.objectStore(storeName).get(key);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async set(storeName, key, value) {
        const db = await this.dbPromise;
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readwrite");
            tx.objectStore(storeName).put(value, key);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async delete(storeName, key) {
        const db = await this.dbPromise;
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readwrite");
            tx.objectStore(storeName).delete(key);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async getAllKeys(storeName) {
        const db = await this.dbPromise;
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readonly");
            const request = tx.objectStore(storeName).getAllKeys();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }
}

export class IndexedDBMemorySaver extends BaseCheckpointSaver {
    constructor(serde) {
        super(serde);
        this.db = new IDBStore();
    }

    async _migratePendingSends(mutableCheckpoint, threadId, checkpointNs, parentCheckpointId) {
        const deseriablizableCheckpoint = mutableCheckpoint;
        const parentKey = _generateKey(threadId, checkpointNs, parentCheckpointId);
        
        const outerWrites = (await this.db.get("writes", parentKey)) || {};
        const pendingSends = await Promise.all(
            Object.values(outerWrites)
                .filter(([_taskId, channel]) => channel === TASKS)
                .map(async ([_taskId, _channel, writes]) => await this.serde.loadsTyped("json", writes))
        );
        
        deseriablizableCheckpoint.channel_values ??= {};
        deseriablizableCheckpoint.channel_values[TASKS] = pendingSends;
        deseriablizableCheckpoint.channel_versions ??= {};
        deseriablizableCheckpoint.channel_versions[TASKS] = Object.keys(deseriablizableCheckpoint.channel_versions).length > 0 
            ? maxChannelVersion(...Object.values(deseriablizableCheckpoint.channel_versions)) 
            : this.getNextVersion(undefined);
    }

    async getTuple(config) {
        const thread_id = config.configurable?.thread_id;
        const checkpoint_ns = config.configurable?.checkpoint_ns ?? "";
        let checkpoint_id = getCheckpointId(config);
        
        let checkpointsMap = await this.db.get("storage", thread_id) || {};
        let nsCheckpoints = checkpointsMap[checkpoint_ns] || {};

        if (checkpoint_id) {
            const saved = nsCheckpoints[checkpoint_id];
            if (saved !== undefined) {
                const [checkpoint, metadata, parentCheckpointId] = saved;
                const key = _generateKey(thread_id, checkpoint_ns, checkpoint_id);
                const deserializedCheckpoint = await this.serde.loadsTyped("json", checkpoint);
                
                if (deserializedCheckpoint.v < 4 && parentCheckpointId !== undefined) {
                    await this._migratePendingSends(deserializedCheckpoint, thread_id, checkpoint_ns, parentCheckpointId);
                }
                
                const outerWrites = await this.db.get("writes", key) || {};
                const pendingWrites = await Promise.all(Object.values(outerWrites).map(async ([taskId, channel, value]) => {
                    return [taskId, channel, await this.serde.loadsTyped("json", value)];
                }));
                
                const checkpointTuple = {
                    config,
                    checkpoint: deserializedCheckpoint,
                    metadata: await this.serde.loadsTyped("json", metadata),
                    pendingWrites
                };
                
                if (parentCheckpointId !== undefined) {
                    checkpointTuple.parentConfig = { configurable: { thread_id, checkpoint_ns, checkpoint_id: parentCheckpointId } };
                }
                return checkpointTuple;
            }
        } else {
            if (Object.keys(nsCheckpoints).length > 0) {
                checkpoint_id = Object.keys(nsCheckpoints).sort((a, b) => b.localeCompare(a))[0];
                const [checkpoint, metadata, parentCheckpointId] = nsCheckpoints[checkpoint_id];
                const key = _generateKey(thread_id, checkpoint_ns, checkpoint_id);
                const deserializedCheckpoint = await this.serde.loadsTyped("json", checkpoint);
                
                if (deserializedCheckpoint.v < 4 && parentCheckpointId !== undefined) {
                    await this._migratePendingSends(deserializedCheckpoint, thread_id, checkpoint_ns, parentCheckpointId);
                }
                
                const outerWrites = await this.db.get("writes", key) || {};
                const pendingWrites = await Promise.all(Object.values(outerWrites).map(async ([taskId, channel, value]) => {
                    return [taskId, channel, await this.serde.loadsTyped("json", value)];
                }));
                
                const checkpointTuple = {
                    config: { configurable: { thread_id, checkpoint_id, checkpoint_ns } },
                    checkpoint: deserializedCheckpoint,
                    metadata: await this.serde.loadsTyped("json", metadata),
                    pendingWrites
                };
                
                if (parentCheckpointId !== undefined) {
                    checkpointTuple.parentConfig = { configurable: { thread_id, checkpoint_ns, checkpoint_id: parentCheckpointId } };
                }
                return checkpointTuple;
            }
        }
    }

    async *list(config, options) {
        let { before, limit, filter } = options ?? {};
        const configThreadId = config.configurable?.thread_id;
        const threadIds = configThreadId ? [configThreadId] : await this.db.getAllKeys("storage");
        const configCheckpointNamespace = config.configurable?.checkpoint_ns;
        const configCheckpointId = config.configurable?.checkpoint_id;

        for (const threadId of threadIds) {
            const threadData = await this.db.get("storage", threadId) || {};
            for (const checkpointNamespace of Object.keys(threadData)) {
                if (configCheckpointNamespace !== undefined && checkpointNamespace !== configCheckpointNamespace) continue;
                
                const checkpoints = threadData[checkpointNamespace] ?? {};
                const sortedCheckpoints = Object.entries(checkpoints).sort((a, b) => b[0].localeCompare(a[0]));
                
                for (const [checkpointId, [checkpoint, metadataStr, parentCheckpointId]] of sortedCheckpoints) {
                    if (configCheckpointId && checkpointId !== configCheckpointId) continue;
                    if (before && before.configurable?.checkpoint_id && checkpointId >= before.configurable.checkpoint_id) continue;
                    
                    const metadata = await this.serde.loadsTyped("json", metadataStr);
                    if (filter && !Object.entries(filter).every(([key, value]) => metadata[key] === value)) continue;
                    
                    if (limit !== undefined) {
                        if (limit <= 0) break;
                        limit -= 1;
                    }
                    
                    const key = _generateKey(threadId, checkpointNamespace, checkpointId);
                    const outerWrites = await this.db.get("writes", key) || {};
                    const writes = Object.values(outerWrites);
                    const pendingWrites = await Promise.all(writes.map(async ([taskId, channel, value]) => {
                        return [taskId, channel, await this.serde.loadsTyped("json", value)];
                    }));
                    
                    const deserializedCheckpoint = await this.serde.loadsTyped("json", checkpoint);
                    if (deserializedCheckpoint.v < 4 && parentCheckpointId !== undefined) {
                        await this._migratePendingSends(deserializedCheckpoint, threadId, checkpointNamespace, parentCheckpointId);
                    }
                    
                    const checkpointTuple = {
                        config: { configurable: { thread_id: threadId, checkpoint_ns: checkpointNamespace, checkpoint_id: checkpointId } },
                        checkpoint: deserializedCheckpoint,
                        metadata,
                        pendingWrites
                    };
                    
                    if (parentCheckpointId !== undefined) {
                        checkpointTuple.parentConfig = { configurable: { thread_id: threadId, checkpoint_ns: checkpointNamespace, checkpoint_id: parentCheckpointId } };
                    }
                    yield checkpointTuple;
                }
            }
        }
    }

    async put(config, checkpoint, metadata) {
        const preparedCheckpoint = copyCheckpoint(checkpoint);
        const threadId = config.configurable?.thread_id;
        const checkpointNamespace = config.configurable?.checkpoint_ns ?? "";
        
        if (threadId === undefined) throw new Error("Missing thread_id");
        
        let threadData = await this.db.get("storage", threadId) || {};
        if (!threadData[checkpointNamespace]) threadData[checkpointNamespace] = {};
        
        const [[, serializedCheckpoint], [, serializedMetadata]] = await Promise.all([
            this.serde.dumpsTyped(preparedCheckpoint), 
            this.serde.dumpsTyped(metadata)
        ]);
        
        threadData[checkpointNamespace][checkpoint.id] = [
            serializedCheckpoint,
            serializedMetadata,
            config.configurable?.checkpoint_id
        ];
        
        await this.db.set("storage", threadId, threadData);
        
        return { configurable: { thread_id: threadId, checkpoint_ns: checkpointNamespace, checkpoint_id: checkpoint.id } };
    }

    async putWrites(config, writes, taskId) {
        const threadId = config.configurable?.thread_id;
        const checkpointNamespace = config.configurable?.checkpoint_ns;
        const checkpointId = config.configurable?.checkpoint_id;
        
        if (threadId === undefined) throw new Error("Missing thread_id");
        if (checkpointId === undefined) throw new Error("Missing checkpoint_id");
        
        const outerKey = _generateKey(threadId, checkpointNamespace, checkpointId);
        let outerWrites_ = await this.db.get("writes", outerKey) || {};
        
        await Promise.all(writes.map(async ([channel, value], idx) => {
            const [, serializedValue] = await this.serde.dumpsTyped(value);
            const innerKey = [taskId, WRITES_IDX_MAP[channel] || idx];
            const innerKeyStr = `${innerKey[0]},${innerKey[1]}`;
            
            if (innerKey[1] >= 0 && innerKeyStr in outerWrites_) return;
            outerWrites_[innerKeyStr] = [taskId, channel, serializedValue];
        }));
        
        await this.db.set("writes", outerKey, outerWrites_);
    }

    async deleteThread(threadId) {
        await this.db.delete("storage", threadId);
        const writeKeys = await this.db.getAllKeys("writes");
        for (const key of writeKeys) {
            if (_parseKey(key).threadId === threadId) {
                await this.db.delete("writes", key);
            }
        }
    }
}
