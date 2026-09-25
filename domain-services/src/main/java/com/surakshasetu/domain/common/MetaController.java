package com.surakshasetu.domain.common;

import com.surakshasetu.domain.contract.MetaApi;
import com.surakshasetu.domain.contract.model.Versions;
import java.util.List;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

/** The versions a new session pins. Rules, params, ranker and rating are stubs until Step 6. */
@RestController
class MetaController implements MetaApi {

  private final Versions versions;

  MetaController(
      @Value("${surakshasetu.versions.rules}") String rules,
      @Value("${surakshasetu.versions.params}") String params,
      @Value("${surakshasetu.versions.ranker}") String ranker,
      @Value("${surakshasetu.versions.registry}") String registry,
      @Value("${surakshasetu.versions.rating}") String rating) {
    this.versions = new Versions(rules, params, ranker, registry, rating, List.of(rules));
  }

  @Override
  public ResponseEntity<Versions> getVersions() {
    return ResponseEntity.ok(versions);
  }
}
