package com.surakshasetu.domain.common;

import com.surakshasetu.domain.contract.MetaApi;
import com.surakshasetu.domain.contract.model.Versions;
import com.surakshasetu.domain.quote.RatingEngine;
import com.surakshasetu.domain.ranking.Ranker;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

/**
 * The versions a new session pins. Rules and params come from the newest loaded DMN release, the
 * ranker from that release's weights file, and rating from the rate table.
 */
@RestController
class MetaController implements MetaApi {

  private final Versions versions;

  MetaController(
      Rules rules,
      Ranker ranker,
      RatingEngine rating,
      @Value("${surakshasetu.versions.registry}") String registry) {
    Rules.Release current = rules.current();
    this.versions =
        new Versions(
            current.rulesVersion(),
            current.paramsVersion(),
            ranker.weights(current.rulesVersion()).rankerVersion(),
            registry,
            rating.version(),
            rules.active());
  }

  @Override
  public ResponseEntity<Versions> getVersions() {
    return ResponseEntity.ok(versions);
  }
}
