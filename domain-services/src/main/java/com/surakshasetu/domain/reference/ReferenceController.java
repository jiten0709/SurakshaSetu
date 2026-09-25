package com.surakshasetu.domain.reference;

import static com.surakshasetu.domain.common.Problems.notFound;

import com.surakshasetu.domain.contract.ReferenceApi;
import com.surakshasetu.domain.contract.model.Occupation;
import com.surakshasetu.domain.contract.model.PincodeInfo;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import org.jspecify.annotations.Nullable;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.RestController;

/** Pincode and occupation masters (catalog schema), read as domain_rw. */
@RestController
class ReferenceController implements ReferenceApi {

  private final JdbcClient jdbc;

  ReferenceController(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  @Override
  public ResponseEntity<PincodeInfo> getPincode(String pincode) {
    return ResponseEntity.ok(
        jdbc.sql(
                "SELECT pincode, district, state, serviceable, is_dummy FROM catalog.pincode"
                    + " WHERE pincode = ?")
            .param(pincode)
            .query(
                (rs, n) ->
                    new PincodeInfo(
                        rs.getString(1),
                        rs.getString(2),
                        rs.getString(3),
                        rs.getBoolean(4),
                        rs.getBoolean(5)))
            .optional()
            .orElseThrow(() -> notFound("unknown pincode")));
  }

  /** Case-insensitive substring match on code or label; every occupation when q is absent. */
  @Override
  public ResponseEntity<List<Occupation>> searchOccupations(@Nullable String q) {
    return ResponseEntity.ok(
        jdbc.sql(
                """
                SELECT code, label, risk_class, is_dummy FROM catalog.occupation
                WHERE CAST(:q AS text) IS NULL
                   OR position(lower(:q) IN lower(code || ' ' || label)) > 0
                ORDER BY code
                """)
            .param("q", q)
            .query(ReferenceController::occupation)
            .list());
  }

  @Override
  public ResponseEntity<Occupation> getOccupation(String code) {
    return ResponseEntity.ok(
        jdbc.sql("SELECT code, label, risk_class, is_dummy FROM catalog.occupation WHERE code = ?")
            .param(code)
            .query(ReferenceController::occupation)
            .optional()
            .orElseThrow(() -> notFound("unknown occupation code")));
  }

  private static Occupation occupation(ResultSet rs, int row) throws SQLException {
    return new Occupation(rs.getString(1), rs.getString(2), rs.getInt(3), rs.getBoolean(4));
  }
}
