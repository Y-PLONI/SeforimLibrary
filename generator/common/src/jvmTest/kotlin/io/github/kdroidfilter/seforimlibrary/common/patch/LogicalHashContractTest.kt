package io.github.kdroidfilter.seforimlibrary.common.patch

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.sql.Connection
import java.sql.DriverManager
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Shared oracle with the Dart client (`contract.yml` cmp's the two fixtures).
 * A failure here means the two hashers drifted and deltas would be rejected.
 */
class LogicalHashContractTest {

    private val fixture = Json.parseToJsonElement(
        requireNotNull(javaClass.getResourceAsStream("/logical_hash_contract.json")) {
            "logical_hash_contract.json missing from test resources"
        }.bufferedReader(Charsets.UTF_8).readText(),
    ).jsonObject

    private val schemaVersion = fixture.getValue("schemaVersion").jsonPrimitive.int
    private val hashTableOrder =
        fixture.getValue("hashTableOrder").jsonArray.map { it.jsonPrimitive.content }
    private val setupSql = fixture.getValue("setupSql").jsonArray.map { it.jsonPrimitive.content }
    private val wholeHash = fixture.getValue("wholeHash").jsonPrimitive.content
    private val tableHashes = fixture.getValue("tableHashes").jsonObject
        .mapValues { it.value.jsonPrimitive.content }

    private fun <T> withFixtureDb(block: (Connection) -> T): T =
        DriverManager.getConnection("jdbc:sqlite::memory:").use { conn ->
            conn.createStatement().use { st -> setupSql.forEach { st.execute(it) } }
            block(conn)
        }

    @Test
    fun `hash table order matches the fixture`() {
        assertEquals(hashTableOrder, LogicalContentHasher.tablesForSchemaVersion(schemaVersion))
    }

    @Test
    fun `whole hash and every per-table hash match the shared oracle`() {
        val report = withFixtureDb {
            LogicalContentHasher.forSchemaVersion(schemaVersion).computeReport(it)
        }
        assertEquals(wholeHash, report.wholeHash, "whole hash")
        assertEquals(hashTableOrder, report.tableHashes.keys.toList(), "per-table key order")
        for (table in hashTableOrder) {
            assertEquals(tableHashes[table], report.tableHashes[table], "table hash for '$table'")
        }
    }

    @Test
    fun `compute is unchanged by the report API`() {
        val (whole, report) = withFixtureDb {
            val hasher = LogicalContentHasher.forSchemaVersion(schemaVersion)
            hasher.compute(it) to hasher.computeReport(it)
        }
        assertEquals(whole, report.wholeHash)
        assertEquals(wholeHash, whole)
    }

    @Test
    fun `absent tables still contribute a hash and distinct tables differ`() {
        val report = withFixtureDb {
            LogicalContentHasher.forSchemaVersion(schemaVersion).computeReport(it)
        }
        // `topic` is absent from the fixture DB — it hashes its prefix only,
        // which must still differ from another absent table's prefix hash.
        assertTrue(report.tableHashes.getValue("topic").isNotEmpty())
        assertTrue(report.tableHashes.getValue("topic") != report.tableHashes.getValue("pub_place"))
    }

    @Test
    fun `a table's hash is the digest of its stream alone`() {
        // A one-table hasher's whole hash must equal that table's entry in the
        // full run: wholeHash is the digest of the concatenated table streams.
        withFixtureDb { conn ->
            val full = LogicalContentHasher.forSchemaVersion(schemaVersion).computeReport(conn)
            for (table in hashTableOrder) {
                val single = LogicalContentHasher(listOf(table)).computeReport(conn)
                assertEquals(full.tableHashes[table], single.wholeHash, "stream digest for '$table'")
                assertEquals(single.wholeHash, single.tableHashes.getValue(table))
            }
        }
    }
}
