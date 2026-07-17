# Runbook: Clearing a Stuck System Update Lock

**Symptom:** HAC → Update Running System fails immediately with:
```
System update can not be performed, there is already existing lock:
'System update' on 'master' tenant and cluster id X, issued at YYYY-MM-DD HH:mm:ss
```
Restarting the backoffice pod does NOT fix it — the lock is in the database.

---

## Investigation path (2026-04-28 / 2026-04-29, D1)

### Step 1 — Check `props` table (dead end)

```groovy
import de.hybris.platform.core.Registry

def conn = Registry.getCurrentTenant().getDataSource().getConnection()
try {
    // First: discover actual column names (propname does NOT exist)
    def cols = conn.getMetaData().getColumns(null, null, "props", null)
    while (cols.next()) println "${cols.getString('COLUMN_NAME')} (${cols.getString('TYPE_NAME')})"
    cols.close()

    // Then query using correct column names
    def rs = conn.createStatement().executeQuery("""
        SELECT TOP 20 NAME, VALUESTRING1
        FROM props
        WHERE LOWER(NAME) LIKE '%lock%'
           OR LOWER(NAME) LIKE '%update%'
           OR LOWER(ISNULL(VALUESTRING1, '')) LIKE '%system update%'
    """)
    while (rs.next()) println "${rs.getString(1)} = ${rs.getString(2)}"
    rs.close()
} finally { conn.close() }
```
**Result:** empty — lock not in `props`.

---

### Step 2 — Discover lock-related tables

```groovy
import de.hybris.platform.core.Registry

def conn = Registry.getCurrentTenant().getDataSource().getConnection()
try {
    def rs = conn.getMetaData().getTables(null, null, null, ["TABLE"] as String[])
    while (rs.next()) {
        def name = rs.getString("TABLE_NAME").toLowerCase()
        if (name.contains("lock") || name.contains("init") || name.contains("cluster") || name.contains("setup")) {
            println rs.getString("TABLE_NAME")
        }
    }
    rs.close()
} finally { conn.close() }
```
**Result:**
```
applicationresourcelock
SYSTEMINIT
systemsetupaudi120sn
systemsetupaudit
```

---

### Step 3 — Check `applicationresourcelock` (dead end for System Update)

```groovy
import de.hybris.platform.core.Registry

def conn = Registry.getCurrentTenant().getDataSource().getConnection()
try {
    def rs = conn.createStatement().executeQuery("""
        SELECT PK, p_region, p_lockkey, p_clusterid, p_timestamp, createdTS, modifiedTS
        FROM applicationresourcelock
    """)
    while (rs.next()) {
        println "region: ${rs.getString('p_region')} | key: ${rs.getString('p_lockkey')} | clusterId: ${rs.getInt('p_clusterid')} | ts: ${rs.getTimestamp('p_timestamp')}"
    }
    rs.close()
} finally { conn.close() }
```
**Result:** only `yCloudHotfolders` lock — not the System Update lock.

---

### Step 4 — Root cause found: `SYSTEMINIT` table

```groovy
import de.hybris.platform.core.Registry

def conn = Registry.getCurrentTenant().getDataSource().getConnection()
try {
    def rs = conn.createStatement().executeQuery("SELECT * FROM SYSTEMINIT")
    def meta = rs.getMetaData()
    while (rs.next()) {
        (1..meta.columnCount).each { i -> print "${meta.getColumnName(i)}: ${rs.getString(i)} | " }
        println ""
    }
    rs.close()
} finally { conn.close() }
```
**Result:**
```
id: globalID | locked: 1 | tenantId: master | clusterNode: 4 | lockdate: 2026-04-27 12:34:10.57 | process: System update | instanceId: 77386670788016
```

---

## Fix

```groovy
import de.hybris.platform.core.Registry

def conn = Registry.getCurrentTenant().getDataSource().getConnection()
try {
    def updated = conn.createStatement().executeUpdate("""
        UPDATE SYSTEMINIT
        SET locked = 0, clusterNode = NULL, lockdate = NULL, process = NULL, instanceId = NULL
        WHERE id = 'globalID' AND tenantId = 'master'
    """)
    println "Updated: ${updated} row(s)"
} finally { conn.close() }
```

Then retry HAC → Update Running System. The lock is gone and System Update proceeds.

---

## Root cause summary

| Table | Columns | Purpose |
|---|---|---|
| `SYSTEMINIT` | `id, locked, tenantId, clusterNode, lockdate, process, instanceId` | Hybris global System Update / Initialize lock |
| `applicationresourcelock` | `p_region, p_lockkey, p_clusterid, p_timestamp` | Cluster resource locks (hotfolders, cron jobs, etc.) — NOT System Update |
| `props` | `NAME, LANGPK, VALUESTRING1, VALUE1` | Generic platform properties — NOT used for System Update lock |

**Why pod restart doesn't help:** The lock is persisted in the DB (`SYSTEMINIT.locked=1`). The pod has no in-memory state to clear on restart.

**Trigger:** A System Update that was interrupted (pod crash, timeout, manual kill) leaves `locked=1` in `SYSTEMINIT` without a cleanup handler.

---

## Notes
- Discovered on D1 (2026-04-28/29) after "No Migration + Recreate" deploy
- Lock had been stuck since 2026-04-27 12:34 (previous HAC System Update attempt)
- Same issue expected on any environment after a failed/interrupted System Update
