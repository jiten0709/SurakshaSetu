package com.surakshasetu.domain.common;

import com.surakshasetu.domain.contract.MetaApi;
import com.surakshasetu.domain.contract.model.Versions;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

/**
 * The versions a new session pins. Rules and params come from the loaded DMN releases (the newest
 * is current); ranker and rating are stubs until Step 7.
 */
@RestController
class MetaController implements MetaApi {

  private final Versions versions;

  MetaController(
      Rules rules,
      @Value("${surakshasetu.versions.ranker}") String ranker,
      @Value("${surakshasetu.versions.registry}") String registry,
      @Value("${surakshasetu.versions.rating}") String rating) {
    Rules.Release current = rules.current();
    this.versions =
        new Versions(
            current.rulesVersion(),
            current.paramsVersion(),
            ranker,
            registry,
            rating,
            rules.active());
  }

  @Override
  public ResponseEntity<Versions> getVersions() {
    return ResponseEntity.ok(versions);
  }
}
