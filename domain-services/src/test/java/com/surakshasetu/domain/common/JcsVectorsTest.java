package com.surakshasetu.domain.common;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HexFormat;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/** The same vector files the orchestrator checks; expected values come from Python's rfc8785. */
class JcsVectorsTest {

  private static final Path VECTORS = Path.of("..", "content", "testvectors", "jcs");
  private static final JsonMapper MAPPER = JsonMapper.builder().build();

  static List<Path> vectors() throws IOException {
    try (Stream<Path> files = Files.list(VECTORS)) {
      return files.filter(p -> p.toString().endsWith(".json")).sorted().toList();
    }
  }

  @Test
  void vectorSetIsComplete() throws IOException {
    assertThat(vectors()).hasSizeGreaterThanOrEqualTo(10);
  }

  @ParameterizedTest
  @MethodSource("vectors")
  void vector(Path path) {
    JsonNode vector = MAPPER.readTree(path.toFile());
    String input = MAPPER.writeValueAsString(vector.get("input"));

    assertThat(HexFormat.of().formatHex(Jcs.canonicalize(input)))
        .isEqualTo(vector.get("jcs_utf8_hex").asString());
    assertThat(Jcs.sha256Hex(input)).isEqualTo(vector.get("sha256_hex").asString());
  }
}
