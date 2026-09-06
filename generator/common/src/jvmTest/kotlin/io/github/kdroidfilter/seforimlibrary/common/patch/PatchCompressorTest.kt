package io.github.kdroidfilter.seforimlibrary.common.patch

import com.github.luben.zstd.Zstd
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.nio.file.Files
import java.security.MessageDigest
import kotlin.random.Random
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals

class PatchCompressorTest {
    @JvmField @Rule
    val tmp = TemporaryFolder()

    private fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    /** Multi-chunk input (> 1 MiB) so the streaming loop crosses chunk boundaries. */
    private fun sampleBytes(size: Int): ByteArray {
        val rnd = Random(42)
        // Mostly repetitive (compressible) with random noise sprinkled in.
        return ByteArray(size) { i -> if (i % 7 == 0) rnd.nextInt().toByte() else (i % 13).toByte() }
    }

    @Test
    fun `frame header carries the exact decompressed size`() {
        val payload = sampleBytes(3 * (1 shl 20) + 12345)
        val patch = tmp.newFile("patch.db").toPath()
        Files.write(patch, payload)

        val result = PatchCompressor.compress(patch, level = 3, workers = 1)
        val compressed = Files.readAllBytes(result.compressedFile)

        // The Otzaria client sizes its buffer from this field; without it, the
        // fallback is compressedSize*20 in one allocation (~11GB for a 560MB patch).
        assertEquals(payload.size.toLong(), Zstd.getFrameContentSize(compressed))
        assertEquals(payload.size.toLong(), result.uncompressedSize)
        assertEquals(compressed.size.toLong(), result.compressedSize)
    }

    @Test
    fun `round-trips and reports matching hashes`() {
        val payload = sampleBytes(2 * (1 shl 20) + 1)
        val patch = tmp.newFile("patch.db").toPath()
        Files.write(patch, payload)

        val result = PatchCompressor.compress(patch, level = 3, workers = 1)
        val compressed = Files.readAllBytes(result.compressedFile)
        val restored = Zstd.decompress(compressed, payload.size)

        assertContentEquals(payload, restored)
        assertEquals(sha256Hex(payload), result.uncompressedSha256)
        assertEquals(sha256Hex(compressed), result.compressedSha256)
    }

    @Test
    fun `multithreaded compression also pledges the size`() {
        val payload = sampleBytes(4 * (1 shl 20))
        val patch = tmp.newFile("patch.db").toPath()
        Files.write(patch, payload)

        val result = PatchCompressor.compress(patch, level = 3, workers = 2)
        val compressed = Files.readAllBytes(result.compressedFile)

        assertEquals(payload.size.toLong(), Zstd.getFrameContentSize(compressed))
        assertContentEquals(payload, Zstd.decompress(compressed, payload.size))
    }

    @Test
    fun `empty patch yields a valid frame with size zero`() {
        val patch = tmp.newFile("patch.db").toPath()
        val result = PatchCompressor.compress(patch, level = 3, workers = 1)
        val compressed = Files.readAllBytes(result.compressedFile)

        assertEquals(0L, Zstd.getFrameContentSize(compressed))
        assertEquals(0, Zstd.decompress(compressed, 0).size)
    }
}
