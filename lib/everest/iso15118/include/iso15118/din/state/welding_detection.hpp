// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Pionix GmbH and Contributors to EVerest
#pragma once

#include "../states.hpp"

namespace iso15118::din::state {

struct WeldingDetection : public StateBase {
    WeldingDetection(Context& ctx) : StateBase(ctx, StateID::WeldingDetection) {
    }

    void enter() final;
    Result feed(Event) final;

private:
    // Arm the V2G_SECC_WeldingDetection supervision timer once, on the first WeldingDetectionReq.
    bool welding_started{false};
};

} // namespace iso15118::din::state
