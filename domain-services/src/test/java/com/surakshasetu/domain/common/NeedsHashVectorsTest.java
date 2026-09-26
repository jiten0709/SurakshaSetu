package com.surakshasetu.domain.common;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Suitability inputs_sha256 = SHA-256(JCS(needs as sent, minus slots_sha256)). The vectors in
 * content/testvectors/needs/ were hashed by the orchestrator's crypto/jcs.py when they were
 * written, and Step 20's Python parity test reads the same files (I2 across tiers).
 */
class NeedsHashVectorsTest {

  private static final Path VECTORS = Path.of("..", "content", "testvectors", "needs");
  private static final JsonMapper JSON = JsonMapper.builder().build();

  @Test
  void javaHashesEveryNeedsVectorLikePython() throws Exception {
    List<Path> files;
    try (Stream<Path> list = Files.list(VECTORS)) {
      files = list.filter(p -> p.toString().endsWith(".json")).sorted().toList();
    }
    assertThat(files).hasSizeGreaterThanOrEqualTo(3);
    for (Path file : files) {
      JsonNode vector = JSON.readTree(Files.readString(file));
      assertThat(Jcs.needsSha256(vector.get("needs")))
          .as(file.getFileName().toString())
          .isEqualTo(vector.get("inputs_sha256").asString());
    }
  }

  @Test
  void theTddVectorMatchesTheJcsPreimageVector() throws Exception {
    JsonNode needs = JSON.readTree(Files.readString(VECTORS.resolve("01-tdd-example.json")));
    JsonNode preimage =
        JSON.readTree(
            Files.readString(
                Path.of("..", "content", "testvectors", "jcs", "12-needs-preimage.json")));
    assertThat(needs.get("inputs_sha256").asString())
        .isEqualTo(preimage.get("sha256_hex").asString())
        .isEqualTo(needs.get("needs").get("slots_sha256").asString());
  }
}
