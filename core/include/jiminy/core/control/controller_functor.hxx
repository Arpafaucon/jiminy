#include <cassert>

#include "jiminy/core/robot/robot.h"


namespace jiminy
{
    template<typename F1, typename F2>
    ControllerFunctor<F1, F2>::ControllerFunctor(F1 & commandFct, F2 & internalDynamicsFct) :
    AbstractController(),
    commandFct_(commandFct),
    internalDynamicsFct_(internalDynamicsFct)
    {
    }

    template<typename F1, typename F2>
    ControllerFunctor<F1, F2>::ControllerFunctor(F1 && commandFct, F2 && internalDynamicsFct) :
    AbstractController(),
    commandFct_(std::move(commandFct)),
    internalDynamicsFct_(std::move(internalDynamicsFct))
    {
    }

    template<typename F1, typename F2>
    hresult_t ControllerFunctor<F1, F2>::computeCommand(
        const float64_t & t, const vectorN_t & q, const vectorN_t & v, vectorN_t & command)
    {
        if (!getIsInitialized())
        {
            PRINT_ERROR("The controller is not initialized.");
            return hresult_t::ERROR_INIT_FAILED;
        }

        commandFct_(t, q, v, sensorsData_, command);

        return hresult_t::SUCCESS;
    }

    template<typename F1, typename F2>
    hresult_t ControllerFunctor<F1, F2>::internalDynamics(
        const float64_t & t, const vectorN_t & q, const vectorN_t & v, vectorN_t & uCustom)
    {
        if (!getIsInitialized())
        {
            PRINT_ERROR("The controller is not initialized.");
            return hresult_t::ERROR_INIT_FAILED;
        }

        // Sensor data are already up-to-date
        internalDynamicsFct_(t, q, v, sensorsData_, uCustom);

        return hresult_t::SUCCESS;
    }
}