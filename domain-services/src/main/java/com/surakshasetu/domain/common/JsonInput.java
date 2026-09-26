package com.surakshasetu.domain.common;

import com.fasterxml.jackson.annotation.JsonSetter;
import com.fasterxml.jackson.annotation.Nulls;
import java.util.List;
import org.springframework.boot.jackson.autoconfigure.JsonMapperBuilderCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * How request JSON binds where the contract's schemas can't be enforced by the generated models
 * (Schemathesis found both as 500s):
 *
 * <ul>
 *   <li>An explicit null binds as if the member were absent, so a non-nullable member keeps its
 *       contract default (e.g. employer_cover_inr "0") instead of reaching the code as null. A
 *       nullable member reads null either way: none has a non-null default.
 *   <li>A null list element is a 400. Map values keep their nulls: a null health flag is a declined
 *       answer.
 * </ul>
 *
 * Hashes are unaffected: inputs_sha256 is taken over the body as sent (common/RawJson).
 */
@Configuration
class JsonInput {

  @Bean
  JsonMapperBuilderCustomizer nullsAtTheBoundary() {
    return builder ->
        builder
            .changeDefaultNullHandling(n -> n.withValueNulls(Nulls.SKIP))
            .withConfigOverride(
                List.class, o -> o.setNullHandling(JsonSetter.Value.forContentNulls(Nulls.FAIL)));
  }
}
